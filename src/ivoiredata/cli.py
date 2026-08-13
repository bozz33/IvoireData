from __future__ import annotations

import argparse
import json

from .delivery import inventory, source_paths
from .discoveries import data_gouv_discoveries
from .engine import IvoireDataEngine
from .query import query_source_sql
from .scheduler import run_forever, run_once


def parser():
    p = argparse.ArgumentParser(prog="ivoiredata", description="IvoireData local source collection, CI Gold and official programming documentation engine")
    p.add_argument("--version", action="version", version="ivoiredata 0.8.3")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sources"); s.add_argument("--public", action="store_true"); s.add_argument("--all", action="store_true", help="include disabled sources")
    s = sub.add_parser("status"); s.add_argument("--public", action="store_true"); s.add_argument("--all", action="store_true", help="include disabled sources")
    sub.add_parser("coverage")
    sub.add_parser("coverage-audit")
    sub.add_parser("quality-audit")
    s = sub.add_parser("upstreams", help="audit persistent upstream cache/version state"); s.add_argument("source_id", nargs="?")
    s = sub.add_parser("discoveries"); s.add_argument("--limit", type=int, default=100, help="maximum unmapped Data.gouv discoveries to display")
    s = sub.add_parser("ci-gold"); s.add_argument("--write", action="store_true", help="write qualification artifacts under data_lake/reports/ci-gold")
    sub.add_parser("inventory")
    s = sub.add_parser("audit"); s.add_argument("--all", action="store_true", help="include controlled/manual sources")
    s = sub.add_parser("source-path"); s.add_argument("source_id")
    s = sub.add_parser("sync"); s.add_argument("source_id", nargs="?"); s.add_argument("--due", action="store_true"); s.add_argument("--all-public", action="store_true"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("scheduler"); s.add_argument("--interval", type=int, default=None); s.add_argument("--once", action="store_true")
    s = sub.add_parser("query"); s.add_argument("source_id"); s.add_argument("sql"); s.add_argument("--max-rows", type=int, default=1000)

    pdocs = sub.add_parser("programming-docs", help="official language/framework documentation corpus")
    pdocs_sub = pdocs.add_subparsers(dest="programming_docs_action", required=True)
    pdocs_sub.add_parser("audit", help="report completeness grouped by programming language")
    pdocs_sub.add_parser("report", help="write programming documentation audit artifacts")
    pdocs_sub.add_parser("languages", help="list registered programming languages and source counts")
    pdocs_sync = pdocs_sub.add_parser("sync", help="synchronize all docs or one programming language")
    pdocs_sync.add_argument("--language", default=None, help="exact language group, e.g. PHP, Python, Rust, JavaScript, C#")
    pdocs_sync.add_argument("--force", action="store_true", help="check now; unchanged bodies are still not downloaded twice")
    pdocs_sync.add_argument("--due", action="store_true", help="only synchronize sources whose refresh interval is due")

    src = sub.add_parser("source")
    src_subs = src.add_subparsers(dest="source_action", required=True)
    src_subs.add_parser("status").add_argument("source_id")
    src_subs.add_parser("enable").add_argument("source_id")
    src_subs.add_parser("disable").add_argument("source_id")
    src_subs.add_parser("auto").add_argument("source_id")
    src_subs.add_parser("manual").add_argument("source_id")
    s_refresh = src_subs.add_parser("refresh"); s_refresh.add_argument("source_id"); s_refresh.add_argument("hours", type=int)

    upd = sub.add_parser("updates")
    upd_subs = upd.add_subparsers(dest="updates_action", required=True)
    upd_subs.add_parser("status")
    upd_subs.add_parser("enable")
    upd_subs.add_parser("disable")
    s_interval = upd_subs.add_parser("interval"); s_interval.add_argument("seconds", type=int)

    qual = sub.add_parser("qualification")
    qual_subs = qual.add_subparsers(dest="qualification_action", required=True)
    qual_subs.add_parser("status")
    qual_subs.add_parser("start")
    qual_subs.add_parser("reset")
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
        "metadata": manifest.get("metadata", {}),
    }


def _source_control(engine: IvoireDataEngine, args) -> int:
    sid = args.source_id
    engine.registry.get(sid)
    action = args.source_action
    if action == "status":
        print(json.dumps(engine.runtime.source_status(engine.registry, sid), indent=2, ensure_ascii=False))
        return 0
    try:
        if action == "enable": engine.runtime.set_source(sid, enabled=True)
        elif action == "disable": engine.runtime.set_source(sid, enabled=False)
        elif action == "auto": engine.runtime.set_source(sid, enabled=True, auto_sync=True)
        elif action == "manual": engine.runtime.set_source(sid, enabled=True, auto_sync=False)
        elif action == "refresh": engine.runtime.set_source(sid, refresh_hours=int(args.hours))
        else: raise SystemExit(f"unknown source action: {action}")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    updated = IvoireDataEngine()
    print(json.dumps(updated.runtime.source_status(updated.registry, sid), indent=2, ensure_ascii=False))
    return 0


def _updates_control(engine: IvoireDataEngine, args) -> int:
    action = args.updates_action
    if action == "status":
        print(json.dumps(engine.runtime.status(engine.registry), indent=2, ensure_ascii=False)); return 0
    try:
        if action == "enable": engine.runtime.set_updates(automatic_enabled=True)
        elif action == "disable": engine.runtime.set_updates(automatic_enabled=False)
        elif action == "interval": engine.runtime.set_updates(scheduler_interval_seconds=int(args.seconds))
        else: raise SystemExit(f"unknown updates action: {action}")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    updated = IvoireDataEngine()
    print(json.dumps(updated.runtime.status(updated.registry), indent=2, ensure_ascii=False))
    return 0


def _qualification_control(engine: IvoireDataEngine, args) -> int:
    action = args.qualification_action
    if action == "status": payload = engine.qualification.status()
    elif action in {"start", "reset"}: payload = engine.start_qualification()
    else: raise SystemExit(f"unknown qualification action: {action}")
    print(json.dumps(payload, indent=2, ensure_ascii=False)); return 0


def _programming_docs_control(engine: IvoireDataEngine, args) -> int:
    action = args.programming_docs_action
    if action == "audit":
        print(json.dumps(engine.programming_docs_audit(), indent=2, ensure_ascii=False, default=str))
        return 0
    if action == "report":
        print(json.dumps(engine.write_programming_docs_report(), indent=2, ensure_ascii=False, default=str))
        return 0
    if action == "languages":
        audit = engine.programming_docs_audit()
        payload = {
            language: {
                "sources": info.get("sources", 0),
                "complete_sources": info.get("complete_sources", 0),
                "selected_pages": info.get("selected_pages", 0),
                "complete": info.get("complete", False),
            }
            for language, info in audit.get("by_language", {}).items()
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if action == "sync":
        results = engine.sync_programming_docs(language=args.language, force=args.force, due_only=args.due)
        if args.language and not results:
            known = sorted(engine.programming_docs_audit().get("by_language", {}).keys())
            raise SystemExit(f"no programming documentation source registered for {args.language!r}; known languages: {', '.join(known)}")
        failed = 0
        for result in results:
            print(json.dumps(result.__dict__, ensure_ascii=False)); failed += result.status != "success"
        return 1 if failed else 0
    raise SystemExit(f"unknown programming-docs action: {action}")


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    engine = IvoireDataEngine()
    if args.command == "sources":
        items = engine.registry.all() if args.all else engine.registry.list()
        if args.public: items = [s for s in items if s.public]
        for spec in items: print(json.dumps(spec.__dict__, ensure_ascii=False))
        return 0
    if args.command == "status":
        items = engine.registry.all() if args.all else engine.registry.list()
        if args.public: items = [s for s in items if s.public]
        for spec in items:
            state = engine.freshness.data.get(spec.source_id, {})
            row = {
                "source_id": spec.source_id, "domain": spec.domain, "connector": spec.connector,
                "enabled": spec.enabled, "refresh_hours": spec.refresh_hours, "auto_sync": spec.auto_sync,
                "due": engine.freshness.due(spec) if spec.enabled else False,
                "last_success": state.get("last_success"), "last_status": state.get("last_status", "never"),
            }
            row.update(_manifest_summary(engine, spec)); print(json.dumps(row, ensure_ascii=False))
        return 0
    if args.command == "coverage": print(json.dumps(engine.coverage(), ensure_ascii=False, indent=2)); return 0
    if args.command == "coverage-audit": print(json.dumps(engine.coverage_audit(), ensure_ascii=False, indent=2)); return 0
    if args.command == "quality-audit": print(json.dumps(engine.quality_audit(), ensure_ascii=False, indent=2)); return 0
    if args.command == "upstreams": print(json.dumps(engine.upstream_audit(args.source_id), ensure_ascii=False, indent=2, default=str)); return 0
    if args.command == "discoveries": print(json.dumps(data_gouv_discoveries(engine, limit=args.limit), ensure_ascii=False, indent=2)); return 0
    if args.command == "ci-gold":
        payload = engine.write_ci_gold() if args.write else engine.ci_gold()
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str)); return 0
    if args.command == "programming-docs": return _programming_docs_control(engine, args)
    if args.command == "inventory": print(json.dumps(inventory(engine.settings, engine.registry.list()), ensure_ascii=False, indent=2)); return 0
    if args.command == "audit": print(json.dumps(engine.audit(public_only=not args.all), ensure_ascii=False, indent=2)); return 0
    if args.command == "source-path":
        spec = engine.registry.get(args.source_id)
        print(json.dumps({k: str(v) for k, v in source_paths(engine.settings, spec).items()}, ensure_ascii=False, indent=2)); return 0
    if args.command == "sync":
        if args.source_id: results = [engine.sync(args.source_id, force=args.force)]
        elif args.due or args.all_public: results = engine.sync_due(auto_only=not args.all_public, public_only=True, force=args.force)
        else: raise SystemExit("sync requires source_id, --due or --all-public")
        failed = 0
        for result in results:
            print(json.dumps(result.__dict__, ensure_ascii=False)); failed += result.status != "success"
        return 1 if failed else 0
    if args.command == "scheduler":
        if args.once:
            results = run_once()
            for result in results: print(json.dumps(result.__dict__, ensure_ascii=False))
            return 1 if any(r.status != "success" for r in results) else 0
        run_forever(args.interval); return 0
    if args.command == "query":
        print(json.dumps(query_source_sql(args.source_id, args.sql, max_rows=args.max_rows), ensure_ascii=False, default=str, indent=2)); return 0
    if args.command == "source": return _source_control(engine, args)
    if args.command == "updates": return _updates_control(engine, args)
    if args.command == "qualification": return _qualification_control(engine, args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
