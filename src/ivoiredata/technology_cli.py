from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from . import technology_maven_authority as _technology_maven_authority
from .http_client import HttpBudgetExceeded, http_run_context
from .settings import Settings
from .technology_authority import OfficialAuthorityResolver
from .technology_catalog import GlobalTechnologyCatalogEngine
from .technology_crates import CratesIndexHarvester
from .technology_documentation import DocumentationTargetResolver
from .technology_documentation_fetch import DynamicDocumentationFetcher
from .technology_go import GoModuleIndexHarvester
from .technology_harvester import RegistryHarvester, TechnologyHarvestQueue
from .technology_maven_runtime import MavenCentralIndexHarvester
from .technology_nuget import NuGetCatalogHarvester
from .technology_qualification import TechnologyQualificationEngine
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
    harvest.add_argument("registry", help="npm, crates, nuget, go, maven, packagist, packagist-changes, pypi, rubygems, pub")
    harvest.add_argument(
        "--limit",
        type=int,
        default=500,
        help=(
            "bounded candidate/event target; npm/crates bound bootstrap names, NuGet bounds "
            "Catalog leaves, Go bounds module-version records, Maven bounds artifact index "
            "events. 0 continues until the current snapshot is complete."
        ),
    )
    harvest.add_argument(
        "--full",
        action="store_true",
        help=(
            "use the complete bulk source where supported; npm uses official _all_docs, "
            "crates uses the official crates.io Git index snapshot, NuGet uses the V3 "
            "Catalog, Go uses index.golang.org with include=all, and Maven uses Central's "
            "official Maven Indexer full chunk before enabling its incremental chain"
        ),
    )
    harvest.add_argument("--reset", action="store_true", help="clear the source cursor/completion state before harvesting")

    qualify = sub.add_parser(
        "qualify",
        help="stage-1 native qualification/prioritization of harvested candidates without inflating the JSON catalog",
    )
    qualify.add_argument(
        "--limit",
        type=int,
        default=50,
        help="positive bounded batch size; deliberately no unlimited mode for multi-million-package qualification",
    )
    qualify.add_argument("--registry", default=None, help="optional registry/ecosystem filter")
    qualify.add_argument("--min-priority", type=int, default=0, help="ignore candidates below this discovery priority")
    qualify.add_argument("--fast-only", action="store_true", help="process only high-priority change/seed candidates")

    qualification_audit = sub.add_parser(
        "qualification-audit",
        help="audit stage-1 qualification decisions and show the best authority-resolution candidates",
    )
    qualification_audit.add_argument("--top", type=int, default=20)

    authority = sub.add_parser(
        "authority",
        help="stage-2 independent authority cross-check for READY_FOR_AUTHORITY candidates; native metadata is reused locally",
    )
    authority.add_argument("--limit", type=int, default=25, help="positive bounded promoted-candidate batch size")
    authority.add_argument("--registry", default=None, help="optional registry/ecosystem filter")

    authority_audit = sub.add_parser(
        "authority-audit",
        help="audit authority decisions and list verified packages ready for documentation resolution",
    )
    authority_audit.add_argument("--top", type=int, default=20)

    docs_targets = sub.add_parser(
        "documentation-targets",
        help="stage-3 materialize versioned documentation targets from AUTHORITY_VERIFIED packages",
    )
    docs_targets.add_argument("--limit", type=int, default=100, help="positive bounded target-resolution batch size")
    docs_targets.add_argument("--registry", default=None, help="optional registry/ecosystem filter")

    docs_targets_audit = sub.add_parser(
        "documentation-targets-audit",
        help="audit dynamic documentation targets grouped by language/ecosystem",
    )
    docs_targets_audit.add_argument("--top", type=int, default=50)

    docs_fetch = sub.add_parser(
        "documentation-fetch",
        help="stage-4 fetch a bounded set of READY documentation targets through the existing official_docs connector",
    )
    docs_fetch.add_argument(
        "--limit",
        type=int,
        default=1,
        help="positive bounded source count; defaults to one documentation source per run",
    )
    docs_fetch.add_argument("--registry", default=None, help="optional registry/ecosystem filter")
    docs_fetch.add_argument(
        "--force",
        action="store_true",
        help="pass force to the standard official_docs connector; normal runs should leave this disabled",
    )

    docs_fetch_audit = sub.add_parser(
        "documentation-fetch-audit",
        help="audit dynamic documentation fetch coverage, aliases, retries and incomplete backlogs",
    )
    docs_fetch_audit.add_argument("--top", type=int, default=50)

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


def _network_run(
    settings: Settings,
    *,
    label: str,
    operation: Callable[[], Any],
) -> tuple[Any, int]:
    safe_label = "".join(char if char.isalnum() or char in "-_" else "-" for char in label).strip("-")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"tech-{safe_label}-{stamp}-{uuid4().hex[:8]}"
    context = http_run_context(
        source_id=f"technology:{label}",
        run_id=run_id,
        state_dir=settings.state_dir,
        user_agent=settings.user_agent,
        options={},
    )
    try:
        with context:
            payload = operation()
    except HttpBudgetExceeded as exc:
        return {"status": "PARTIAL_BUDGET", "error": str(exc), "http": context.snapshot()}, 2
    metrics = context.snapshot()
    if isinstance(payload, dict):
        payload = {**payload, "http": metrics}
    return payload, 0


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    settings = _settings()
    engine = _engine(settings)
    queue: TechnologyHarvestQueue | None = None
    exit_code = 0
    try:
        if args.command == "package":
            payload, exit_code = _network_run(
                settings,
                label=f"package-{args.registry}",
                operation=lambda: engine.discover_package(args.registry, args.name),
            )
        elif args.command == "languages":
            payload = engine.discover_languages(limit=args.limit)
        elif args.command == "wikidata":
            payload = discover_wikidata_resilient(engine, limit=args.limit)
        elif args.command == "catalog":
            payload = engine.catalog(limit=args.limit, verified_only=args.verified_only, min_importance=args.min_importance)
        elif args.command == "refresh":
            payload, exit_code = _network_run(settings, label="refresh", operation=lambda: engine.refresh_packages(limit=args.limit))
        elif args.command == "harvest":
            queue = _queue(settings)
            key = str(args.registry or "").strip().casefold()
            if key in {"crates", "crate", "cargo", "crates.io"}:
                crates = CratesIndexHarvester(queue=queue)
                operation = lambda: crates.harvest(limit=args.limit, full=args.full, reset=args.reset)
            elif key in {"nuget", "nuget.org"}:
                nuget = NuGetCatalogHarvester(queue=queue, user_agent=settings.user_agent)
                operation = lambda: nuget.harvest(limit=args.limit, full=args.full, reset=args.reset)
            elif key in {"go", "golang", "proxy.golang.org", "index.golang.org"}:
                go_index = GoModuleIndexHarvester(queue=queue, user_agent=settings.user_agent)
                operation = lambda: go_index.harvest(limit=args.limit, full=args.full, reset=args.reset)
            elif key in {"maven", "maven-central", "repo1.maven.org", "repo.maven.apache.org"}:
                operation = lambda: MavenCentralIndexHarvester(
                    queue=queue,
                    user_agent=settings.user_agent,
                    state_dir=settings.state_dir,
                ).harvest(limit=args.limit, full=args.full, reset=args.reset)
            else:
                harvester = RegistryHarvester(queue=queue, user_agent=settings.user_agent)
                operation = lambda: harvester.harvest(args.registry, limit=args.limit, full=args.full, reset=args.reset)
            payload, exit_code = _network_run(settings, label=f"harvest-{args.registry}", operation=operation)
        elif args.command == "qualify":
            queue = _queue(settings)
            qualifier = TechnologyQualificationEngine(queue=queue, user_agent=settings.user_agent)
            payload, exit_code = _network_run(
                settings,
                label="qualify",
                operation=lambda: qualifier.run(
                    limit=args.limit,
                    registry=args.registry,
                    min_priority=args.min_priority,
                    fast_only=args.fast_only,
                ),
            )
        elif args.command == "qualification-audit":
            queue = _queue(settings)
            payload = TechnologyQualificationEngine(queue=queue, user_agent=settings.user_agent).audit(top=args.top)
        elif args.command == "authority":
            queue = _queue(settings)
            resolver = OfficialAuthorityResolver(queue=queue, user_agent=settings.user_agent)
            payload, exit_code = _network_run(
                settings,
                label="authority",
                operation=lambda: resolver.run(limit=args.limit, registry=args.registry),
            )
        elif args.command == "authority-audit":
            queue = _queue(settings)
            payload = OfficialAuthorityResolver(queue=queue, user_agent=settings.user_agent).audit(top=args.top)
        elif args.command == "documentation-targets":
            queue = _queue(settings)
            payload = DocumentationTargetResolver(queue=queue).run(limit=args.limit, registry=args.registry)
        elif args.command == "documentation-targets-audit":
            queue = _queue(settings)
            payload = DocumentationTargetResolver(queue=queue).audit(top=args.top)
        elif args.command == "documentation-fetch":
            queue = _queue(settings)
            fetcher = DynamicDocumentationFetcher(queue=queue, settings=settings)
            payload, exit_code = _network_run(
                settings,
                label="documentation-fetch",
                operation=lambda: fetcher.run(limit=args.limit, registry=args.registry, force=args.force),
            )
        elif args.command == "documentation-fetch-audit":
            queue = _queue(settings)
            payload = DynamicDocumentationFetcher(queue=queue, settings=settings).audit(top=args.top)
        elif args.command == "bootstrap":
            queue = _queue(settings)
            harvester = RegistryHarvester(queue=queue, user_agent=settings.user_agent)

            def do_bootstrap():
                return {
                    "languages": len(engine.discover_languages(limit=args.language_limit)),
                    "wikidata": len(discover_wikidata_resilient(engine, limit=args.wikidata_limit)),
                    "harvest": {
                        "packagist": harvester.harvest("packagist", limit=args.package_limit),
                        "rubygems": harvester.harvest("rubygems", limit=args.package_limit),
                        "pub": harvester.harvest("pub", limit=args.package_limit),
                    },
                }

            payload, exit_code = _network_run(settings, label="bootstrap", operation=do_bootstrap)
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
        return exit_code
    finally:
        if queue is not None:
            queue.close()


if __name__ == "__main__":
    raise SystemExit(main())
