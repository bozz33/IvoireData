from __future__ import annotations

import argparse
import json

from .delivery import inventory, source_paths
from .engine import IvoireDataEngine
from .query import query_sql
from .scheduler import run_forever, run_once


def parser():
    p = argparse.ArgumentParser(prog="ivoiredata", description="IvoireData local source collection and domain delivery engine")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sources"); s.add_argument("--public", action="store_true")
    s = sub.add_parser("status"); s.add_argument("--public", action="store_true")
    sub.add_parser("coverage")
    sub.add_parser("inventory")
    s = sub.add_parser("source-path"); s.add_argument("source_id")
    s = sub.add_parser("sync"); s.add_argument("source_id", nargs="?"); s.add_argument("--due", action="store_true"); s.add_argument("--all-public", action="store_true"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("scheduler"); s.add_argument("--interval", type=int, default=3600); s.add_argument("--once", action="store_true")
    s = sub.add_parser("query"); s.add_argument("sql"); s.add_argument("--max-rows", type=int, default=1000)
    return p


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
            print(json.dumps({"source_id": spec.source_id, "domain": spec.domain, "connector": spec.connector, "refresh_hours": spec.refresh_hours, "auto_sync": spec.auto_sync, "due": engine.freshness.due(spec), "last_success": state.get("last_success"), "last_status": state.get("last_status", "never")}, ensure_ascii=False))
        return 0
    if args.command == "coverage":
        print(json.dumps(engine.coverage(), ensure_ascii=False, indent=2)); return 0
    if args.command == "inventory":
        print(json.dumps(inventory(engine.settings, engine.registry.list()), ensure_ascii=False, indent=2)); return 0
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
        print(json.dumps(query_sql(args.sql, max_rows=args.max_rows), ensure_ascii=False, default=str, indent=2)); return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
