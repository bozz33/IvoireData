from __future__ import annotations

import argparse
import json

from .settings import Settings
from .technology_catalog import GlobalTechnologyCatalogEngine


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ivoiredata-tech",
        description="IvoireData global technology discovery, authority resolution and catalog engine",
    )
    sub = p.add_subparsers(dest="command", required=True)

    package = sub.add_parser("package", help="discover one package using its native registry first, then cross-check sources")
    package.add_argument("registry", help="npm, pypi, packagist, cargo, rubygems, nuget, maven, go, pub, hex, ...")
    package.add_argument("name", help="registry package name")

    languages = sub.add_parser("languages", help="discover programming languages from GitHub Linguist")
    languages.add_argument("--limit", type=int, default=0, help="0 means all programming languages")

    wikidata = sub.add_parser("wikidata", help="discover programming languages/frameworks from Wikidata")
    wikidata.add_argument("--limit", type=int, default=500)

    catalog = sub.add_parser("catalog", help="show the local dynamic technology catalog")
    catalog.add_argument("--limit", type=int, default=100)
    catalog.add_argument("--verified-only", action="store_true")
    catalog.add_argument("--min-importance", type=int, default=0, help="minimum computed importance score (0-100)")

    refresh = sub.add_parser("refresh", help="refresh already known packages using native registries and cross-check sources")
    refresh.add_argument("--limit", type=int, default=0, help="0 means all known package records")

    sub.add_parser("reconcile", help="reconcile package/Wikidata/language identities using strong repository evidence")
    sub.add_parser("audit", help="audit the dynamic technology catalog")
    return p


def _engine() -> GlobalTechnologyCatalogEngine:
    settings = Settings.from_env()
    return GlobalTechnologyCatalogEngine(
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
        payload = engine.catalog(limit=args.limit, verified_only=args.verified_only, min_importance=args.min_importance)
    elif args.command == "refresh":
        payload = engine.refresh_packages(limit=args.limit)
    elif args.command == "reconcile":
        payload = engine.reconcile_identities()
    elif args.command == "audit":
        payload = engine.audit()
    else:
        raise SystemExit(f"unknown technology discovery command: {args.command}")
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
