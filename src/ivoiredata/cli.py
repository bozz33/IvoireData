from __future__ import annotations

import argparse
import json

from .delivery import inventory, source_paths
from .engine import IvoireDataEngine
from .query import query_source_sql
from .scheduler import run_forever, run_once


def parser():
    p = argparse.ArgumentParser(prog="ivoiredata", description="IvoireData local source collection and domain delivery engine")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sources"); s.add_argument("--public", action="store_true")
    s = sub.add_parser("status"); s.add_argument("--public", action="store_true")
    sub.add_parser("coverage")
    sub.add_parser("inventory")
    s = sub.add_parser("audit"); s.add_argument("--all", action="store_true", help="include controlled/manual sources")
    s = sub.add_parser("source-path"); s.add_argument("source_id")
    s = sub.add_parser("sync"); s.add_argument("source_id", nargs="?"); s.add_argument("--due", action="store_true"); s.add_argument("--all-public", action="store_true"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("scheduler"); s.add_argument("--interval", type=int, default=3600); s.add_argument("--once", action="store_true")
    s = sub.add_parser("query"); s.add_argument("source_id"); s.add_argument("sql"); s.add_argument("--max-rows", type=int, default=1000)
    # Contrôle des sources
    src = sub.add_parser("source"); src_subs = src.add_subparsers(dest="source_action", required=True)
    src_subs.add_parser("enable").add_argument("source_id")
    src_subs.add_parser("disable").add_argument("source_id")
    src_subs.add_parser("auto").add_argument("source_id")
    src_subs.add_parser("manual").add_argument("source_id")
    s_refresh = src_subs.add_parser("refresh"); s_refresh.add_argument("source_id"); s_refresh.add_argument("hours", type=int)
    # Contrôle global des mises à jour
    upd = sub.add_parser("updates")
    upd_subs = upd.add_subparsers(dest="updates_action", required=True)
    upd_subs.add_parser("status")
    upd_subs.add_parser("enable")
    upd_subs.add_parser("disable")
    return p


def _manifest_summary(engine: IvoireDataEngine, spec) -> dict:
    path = source_paths(engine.settings, spec)["manifest"]
    if not path.exists():
        return {}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    delivery = manifest.get("delivery", {})
    return {
        "delivery_status": manifest.get("delivery_status") or delivery.get("status"),
        "freshness_status": manifest.get("freshness_status") or manifest.get("freshness", {}).get("status"),
        "transport_security": manifest.get("transport_security") or manifest.get("transport", {}).get("security"),
        "rows": delivery.get("rows", manifest.get("inventory", {}).get("tables", {}).get("rows", 0)),
        "warnings": manifest.get("warnings", []),
    }


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    engine = IvoireDataEngine()
    if args.command == "sources":
        for spec in engine.registry.list(public_only=args.public):
            print(json.dumps(spec.__dict__, ensure_ascii=False))
        return 0
    if args.command == "status":
        for spec in engine.registry.list(public_only=args.public):
            state = engine.freshness.data.get(spec.source_id, {})
            row = {
                "source_id": spec.source_id,
                "domain": spec.domain,
                "connector": spec.connector,
                "refresh_hours": spec.refresh_hours,
                "auto_sync": spec.auto_sync,
                "due": engine.freshness.due(spec),
                "last_success": state.get("last_success"),
                "last_status": state.get("last_status", "never"),
            }
            row.update(_manifest_summary(engine, spec))
            print(json.dumps(row, ensure_ascii=False))
        return 0
    if args.command == "coverage":
        print(json.dumps(engine.coverage(), ensure_ascii=False, indent=2)); return 0
    if args.command == "inventory":
        print(json.dumps(inventory(engine.settings, engine.registry.list()), ensure_ascii=False, indent=2)); return 0
    if args.command == "audit":
        print(json.dumps(engine.audit(public_only=not args.all), ensure_ascii=False, indent=2)); return 0
    if args.command == "source-path":
        spec = engine.registry.get(args.source_id)
        print(json.dumps({k: str(v) for k, v in source_paths(engine.settings, spec).items()}, ensure_ascii=False, indent=2)); return 0
    if args.command == "sync":
        if args.source_id:
            results = [engine.sync(args.source_id, force=args.force)]
        elif args.due or args.all_public:
            results = engine.sync_due(auto_only=not args.all_public, public_only=True, force=args.force)
        else:
            raise SystemExit("sync requires source_id, --due or --all-public")
        failed = 0
        for result in results:
            print(json.dumps(result.__dict__, ensure_ascii=False)); failed += result.status != "success"
        return 1 if failed else 0
    if args.command == "scheduler":
        if args.once:
            results = run_once()
            for result in results:
                print(json.dumps(result.__dict__, ensure_ascii=False))
            return 1 if any(r.status != "success" for r in results) else 0
        run_forever(args.interval); return 0
    if args.command == "query":
        print(json.dumps(query_source_sql(args.source_id, args.sql, max_rows=args.max_rows), ensure_ascii=False, default=str, indent=2)); return 0
    if args.command == "source":
        return _source_control(engine, args)
    if args.command == "updates":
        return _updates_control(engine, args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


def _source_control(engine: IvoireDataEngine, args) -> int:
    """Applique les actions source enable/disable/auto/manual/refresh au runtime config."""
    import json as _json
    from pathlib import Path as _Path

    sid = args.source_id
    spec = engine.registry.get(sid)  # valide l'existence
    config_path = engine.settings.runtime_config_path
    config = _json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config.setdefault("sources", {})
    entry = config["sources"].setdefault(sid, {})

    action = args.source_action
    changes: list[str] = []
    if action == "enable":
        entry["enabled"] = True; changes.append("enabled=true")
    elif action == "disable":
        entry["enabled"] = False; changes.append("enabled=false")
    elif action == "auto":
        entry["enabled"] = True; entry["auto_sync"] = True; changes.append("enabled=true, auto_sync=true")
    elif action == "manual":
        entry["enabled"] = True; entry["auto_sync"] = False; changes.append("enabled=true, auto_sync=false")
    elif action == "refresh":
        hours = int(getattr(args, "hours", 168))
        entry["refresh_hours"] = hours; changes.append(f"refresh_hours={hours}")
    else:
        raise SystemExit(f"unknown source action: {action}")

    config_path.write_text(_json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(_json.dumps({"source_id": sid, "action": action, "changes": changes}, ensure_ascii=False))
    return 0


def _updates_control(engine: IvoireDataEngine, args) -> int:
    """Affiche ou modifie l'état global des mises à jour automatiques."""
    import json as _json
    from pathlib import Path as _Path

    config_path = engine.settings.runtime_config_path
    config = _json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config.setdefault("updates", {})
    updates = config["updates"]

    action = args.updates_action
    if action == "status":
        auto_enabled = bool(updates.get("automatic_enabled", True))
        interval = int(updates.get("scheduler_interval_seconds", 3600))
        specs = engine.registry.list()
        auto_count = sum(1 for s in specs if s.auto_sync)
        manual_count = sum(1 for s in specs if s.enabled and not s.auto_sync)
        disabled_count = sum(1 for s in engine.registry._sources.values() if not s.enabled)
        print(_json.dumps({
            "automatic_enabled": auto_enabled,
            "scheduler_interval_seconds": interval,
            "automatic_sources": auto_count,
            "manual_sources": manual_count,
            "disabled_sources": disabled_count,
        }, indent=2, ensure_ascii=False))
    elif action == "enable":
        updates["automatic_enabled"] = True
        config_path.write_text(_json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(_json.dumps({"status": "Automatic updates enabled. Manual sync remains available."}, ensure_ascii=False))
    elif action == "disable":
        updates["automatic_enabled"] = False
        config_path.write_text(_json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(_json.dumps({"status": "Automatic updates disabled. Manual sync remains available."}, ensure_ascii=False))
    else:
        raise SystemExit(f"unknown updates action: {action}")
    return 0
