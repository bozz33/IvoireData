from __future__ import annotations

import argparse
import json

from .settings import Settings
from .technology_discovery import GlobalTechnologyDiscoveryEngine


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ivoiredata-tech",
        description="IvoireData global technology discovery and official-source catalog engine",
    )
    sub = p.add_subparsers(dest="command", required=True)

    package = sub.add_parser("package", help="discover one package through ecosyste.ms + deps.dev")
    package.add_argument("registry", help="npm, pypi, packagist, cargo, nuget, maven, go, pub, hex, ...")
    package.add_argument("name", help="registry package name")

    languages = sub.add_parser("languages", help="discover programming languages from GitHub Linguist")
    languages.add_argument("--limit", type=int, default=0, help="0 means all programming languages")

    wikidata = sub.add_parser("wikidata", help="discover programming languages/frameworks from Wikidata")
    wikidata.add_argument("--limit", type=int, default=500)

    catalog = sub.add_parser("catalog", help="show the local dynamic technology catalog")
    catalog.add_argument("--limit", type=int, default=100)
    catalog.add_argument("--verified-only", action="store_true")

    sub.add_parser("audit", help="audit the dynamic technology catalog")
    return p


def _engine() -> GlobalTechnologyDiscoveryEngine:
    settings = Settings.from_env()
    return GlobalTechnologyDiscoveryEngine(
        state_path=settings.state_dir / "technology_catalog.json",
        user_agent=settings.user_agent,
    )


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    engine = _engine()
    if args.command == "package":
        payload = engine.discover_package(args.registry, args.name)
    elif args.command == "languages":
        payload = engine.discover_languages(limit=args.limit)
    elif args.command == "wikidata":
        payload = engine.discover_wikidata(limit=args.limit)
    elif args.command == "catalog":
        payload = engine.catalog(limit=args.limit, verified_only=args.verified_only)
    elif args.command == "audit":
        payload = engine.audit()
    else:
        raise SystemExit(f"unknown technology discovery command: {args.command}")
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
