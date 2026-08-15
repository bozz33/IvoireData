from __future__ import annotations

import argparse
import json

from .settings import Settings
from .technology_catalog import GlobalTechnologyCatalogEngine
from .technology_harvester import RegistryHarvester, TechnologyHarvestQueue, qualify_pending
from .technology_wikidata import discover_wikidata_resilient


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ivoiredata-tech",
        description="IvoireData global technology discovery, authority resolution, harvesting and catalog engine",
    )
    sub = p.add_subparsers(dest="command", required=True)

    package = sub.add_parser("package", help="discover one package using its native registry first, then cross-check sources")
    package.add_argument("registry", help="npm, pypi, packagist, cargo, rubygems, nuget, maven, go, pub, hex, ...")
    package.add_argument("name", help="registry package name")

    languages = sub.add_parser("languages", help="discover programming languages from GitHub Linguist")
    languages.add_argument("--limit", type=int, default=0, help="0 means all programming languages")

    wikidata = sub.add_parser("wikidata", help="discover a bounded language/framework seed from Wikidata using resilient split queries")
    wikidata.add_argument("--limit", type=int, default=500)

    catalog = sub.add_parser("catalog", help="show the local dynamic technology catalog")
    catalog.add_argument("--limit", type=int, default=100)
    catalog.add_argument("--verified-only", action="store_true")
    catalog.add_argument("--min-importance", type=int, default=0, help="minimum computed importance score (0-100)")

    refresh = sub.add_parser("refresh", help="refresh already known packages using native registries and cross-check sources")
    refresh.add_argument("--limit", type=int, default=0, help="0 means all known package records")

    harvest = sub.add_parser("harvest", help="discover package names from official bulk/change feeds into the SQLite queue")
    harvest.add_argument("registry", help="packagist, packagist-changes, pypi, rubygems, pub")
    harvest.add_argument("--limit", type=int, default=500, help="bounded candidate target for ranked feeds; full feeds may process an entire server page")
    harvest.add_argument("--full", action="store_true", help="use the complete bulk source where supported instead of the bounded/ranked source")
    harvest.add_argument("--reset", action="store_true", help="clear the source cursor/completion state before harvesting")

    qualify = sub.add_parser("qualify", help="resolve pending harvested candidates through native registries + cross-checks")
    qualify.add_argument("--limit", type=int, default=50)

    bootstrap = sub.add_parser("bootstrap", help="seed languages/Wikidata and a bounded set of official registry candidates")
    bootstrap.add_argument("--language-limit", type=int, default=0, help="0 means all GitHub Linguist programming languages")
    bootstrap.add_argument("--wikidata-limit", type=int, default=500)
    bootstrap.add_argument("--package-limit", type=int, default=200, help="per-registry candidate limit for safe initial bootstrap")

    sub.add_parser("harvest-audit", help="audit the SQLite global candidate queue and cursors")
    sub.add_parser("reconcile", help="reconcile package/Wikidata/language identities using strong repository evidence")
    sub.add_parser("audit", help="audit the dynamic technology catalog")
    return p


def _settings() -> Settings:
    return Settings.from_env()


def _engine(settings: Settings | None = None) -> GlobalTechnologyCatalogEngine:
    settings = settings or _settings()
    return GlobalTechnologyCatalogEngine(
        state_path=settings.state_dir / "technology_catalog.json",
        user_agent=settings.user_agent,
    )


def _queue(settings: Settings) -> TechnologyHarvestQueue:
    return TechnologyHarvestQueue(settings.state_dir / "technology_harvest.sqlite3")


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    settings = _settings()
    engine = _engine(settings)
    queue: TechnologyHarvestQueue | None = None
    try:
        if args.command == "package":
            payload = engine.discover_package(args.registry, args.name)
        elif args.command == "languages":
            payload = engine.discover_languages(limit=args.limit)
        elif args.command == "wikidata":
            payload = discover_wikidata_resilient(engine, limit=args.limit)
        elif args.command == "catalog":
            payload = engine.catalog(limit=args.limit, verified_only=args.verified_only, min_importance=args.min_importance)
        elif args.command == "refresh":
            payload = engine.refresh_packages(limit=args.limit)
        elif args.command == "harvest":
            queue = _queue(settings)
            payload = RegistryHarvester(queue=queue, user_agent=settings.user_agent).harvest(
                args.registry, limit=args.limit, full=args.full, reset=args.reset
            )
        elif args.command == "qualify":
            queue = _queue(settings)
            payload = qualify_pending(queue=queue, catalog_engine=engine, limit=args.limit)
        elif args.command == "bootstrap":
            queue = _queue(settings)
            harvester = RegistryHarvester(queue=queue, user_agent=settings.user_agent)
            payload = {
                "languages": len(engine.discover_languages(limit=args.language_limit)),
                "wikidata": len(discover_wikidata_resilient(engine, limit=args.wikidata_limit)),
                "harvest": {
                    "packagist": harvester.harvest("packagist", limit=args.package_limit),
                    "rubygems": harvester.harvest("rubygems", limit=args.package_limit),
                    "pub": harvester.harvest("pub", limit=args.package_limit),
                },
            }
        elif args.command == "harvest-audit":
            queue = _queue(settings)
            payload = queue.audit()
        elif args.command == "reconcile":
            payload = engine.reconcile_identities()
        elif args.command == "audit":
            payload = engine.audit()
        else:
            raise SystemExit(f"unknown technology discovery command: {args.command}")
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return 0
    finally:
        if queue is not None:
            queue.close()


if __name__ == "__main__":
    raise SystemExit(main())
