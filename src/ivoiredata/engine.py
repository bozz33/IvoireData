from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from typing import Any

from .connectors.bulk_catalog import bulk_catalog_resource
from .connectors.data_gouv_ci import data_gouv_ci_resource, dataset_id_from_public_url
from .connectors.faostat import CATALOG_URL as FAOSTAT_CATALOG_URL, faostat_country_resource
from .connectors.geoboundaries import geoboundaries_resource
from .connectors.http_file import http_file_resource
from .connectors.ilostat import DATA_API as ILOSTAT_DATA_API, ilostat_ref_area_resource
from .connectors.official_docs import official_docs_resource
from .connectors.osm_geofabrik import geofabrik_snapshot_resource
from .connectors.public_web import public_document_resource
from .connectors.uis import uis_country_resource
from .connectors.world_bank import world_bank_wdi_resource
from .connectors.world_bank_projects import world_bank_projects_resource
from .delivery import ensure_source_layout, rebuild_catalog, source_paths, write_source_manifest
from .freshness import FreshnessStore
from .metadata import source_metadata
from .models import SourceSpec, SyncResult
from .pipeline import get_source_pipeline
from .qualification import QualificationStore
from .registry import SourceRegistry
from .runtime_control import RuntimeControl
from .settings import Settings
from .state_io import atomic_write_json, load_json
from .upstream_state import UpstreamState


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_programming_docs(spec: SourceSpec) -> bool:
    return spec.connector == "official_docs"


def _audit_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    delivery: dict[str, int] = {}
    sync: dict[str, int] = {}
    freshness: dict[str, int] = {}
    transport: dict[str, int] = {}
    structured_rows = 0
    document_rows = 0
    total_rows = 0
    for row in rows:
        for bucket, key in ((delivery, "delivery_status"), (sync, "sync_status"), (freshness, "freshness_status"), (transport, "transport")):
            value = str(row.get(key) or "UNKNOWN")
            bucket[value] = bucket.get(value, 0) + 1
        structured_rows += int(row.get("structured_rows") or 0)
        document_rows += int(row.get("document_rows") or 0)
        total_rows += int(row.get("total_rows") or 0)
    return {
        "sources": len(rows),
        "delivery": dict(sorted(delivery.items())),
        "sync": dict(sorted(sync.items())),
        "freshness": dict(sorted(freshness.items())),
        "transport": dict(sorted(transport.items())),
        "structured_rows": structured_rows,
        "document_rows": document_rows,
        "total_rows": total_rows,
    }


class _ScopedRegistry:
    def __init__(self, registry: SourceRegistry, predicate):
        self._registry = registry
        self._predicate = predicate

    def all(self) -> list[SourceSpec]:
        return [spec for spec in self._registry.all() if self._predicate(spec)]

    def list(self, *, public_only: bool = False, auto_only: bool = False) -> list[SourceSpec]:
        return [
            spec for spec in self._registry.list(public_only=public_only, auto_only=auto_only)
            if self._predicate(spec)
        ]

    def get(self, source_id: str) -> SourceSpec:
        spec = self._registry.get(source_id)
        if not self._predicate(spec):
            raise KeyError(f"source outside scoped registry: {source_id}")
        return spec


class _CIGoldEngineView:
    def __init__(self, engine: "IvoireDataEngine"):
        self._engine = engine
        self.settings = engine.settings
        self.registry = _ScopedRegistry(engine.registry, lambda spec: not _is_programming_docs(spec))
        self.qualification = engine.qualification

    def __getattr__(self, name: str):
        return getattr(self._engine, name)

    def audit(self, *, public_only: bool = True) -> dict[str, Any]:
        allowed = {spec.source_id for spec in self.registry.list(public_only=public_only)}
        rows = [row for row in self._engine.audit(public_only=public_only)["rows"] if row["source_id"] in allowed]
        return {"summary": _audit_summary(rows), "rows": rows}

    def upstream_audit(self, source_id: str | None = None) -> dict[str, Any]:
        if source_id is not None:
            self.registry.get(source_id)
            return self._engine.upstream_audit(source_id)
        allowed = {spec.source_id for spec in self.registry.all()}
        payload = self._engine.upstream_audit()
        rows = [row for row in payload.get("rows", []) if row.get("source_id") in allowed]
        totals: dict[str, int] = {}
        for row in rows:
            for result, count in (row.get("last_results") or {}).items():
                totals[result] = totals.get(result, 0) + int(count)
        return {
            "state_path": payload.get("state_path"),
            "summary": {
                "sources": len(rows),
                "artifacts": sum(int(row.get("artifacts") or 0) for row in rows),
                "last_results": dict(sorted(totals.items())),
            },
            "rows": rows,
        }


class IvoireDataEngine:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.runtime = RuntimeControl(self.settings)
        self.registry = SourceRegistry.load(
            self.settings.registry_path,
            self.settings.runtime_config_path,
            self.settings.runtime_overrides_path,
            self.settings.runtime_overlay_paths,
            self.settings.registry_overlay_paths,
        )
        self.freshness = FreshnessStore(self.settings.state_dir / "freshness.json")
        self.qualification = QualificationStore(self.settings.qualification_path)
        self.upstreams = UpstreamState(self.settings.upstream_state_path)

    def _resource_for(self, spec: SourceSpec, *, force: bool = False):
        p = ensure_source_layout(self.settings, spec)
        o = spec.options
        meta = source_metadata(spec)
        upstream_state_path = self.settings.upstream_state_path
        if spec.connector == "data_gouv_ci":
            dsid = None if spec.source_id == "civ_datagouv_catalog" else dataset_id_from_public_url(spec.source_url)
            return data_gouv_ci_resource(
                source_id=spec.source_id,
                dataset_ids=[dsid] if dsid else None,
                force=force,
                user_agent=self.settings.user_agent,
                limit=o.get("limit"),
                snapshot_dir=p["raw"],
                metadata_base=meta,
                upstream_state_path=upstream_state_path,
            )
        if spec.connector == "http_file":
            return http_file_resource(
                source_id=spec.source_id,
                url=spec.source_url,
                force=force,
                user_agent=self.settings.user_agent,
                snapshot_dir=p["raw"],
                upstream_state_path=upstream_state_path,
            )
        if spec.connector == "world_bank_wdi":
            return world_bank_wdi_resource(
                country=str(o.get("country", "CIV")),
                source=int(o.get("source", 2)),
                indicator_limit=o.get("indicator_limit"),
                batch_size=int(o.get("batch_size", 60)),
                user_agent=self.settings.user_agent,
                snapshot_dir=p["raw"],
                metadata_base=meta,
                upstream_state_path=upstream_state_path,
            )
        if spec.connector == "world_bank_projects":
            return world_bank_projects_resource(
                country_code=str(o.get("country_code", "CI")),
                page_size=int(o.get("page_size", 50)),
                user_agent=self.settings.user_agent,
                snapshot_dir=p["raw"],
                upstream_state_path=upstream_state_path,
            )
        if spec.connector == "faostat_country":
            return faostat_country_resource(
                country=str(o.get("country", "CIV")),
                aliases=o.get("aliases", ["Côte d'Ivoire", "Cote d'Ivoire", "Côte d’Ivoire", "Ivory Coast"]),
                datasets=o.get("datasets"),
                dataset_codes=o.get("dataset_codes"),
                catalog_url=str(o.get("catalog_url", FAOSTAT_CATALOG_URL)),
                include_discontinued=bool(o.get("include_discontinued", False)),
                user_agent=self.settings.user_agent,
                snapshot_dir=p["raw"],
                tables_dir=p["tables"],
                upstream_state_path=upstream_state_path,
                max_bytes_per_file=int(o.get("max_bytes_per_file", 500_000_000)),
                max_new_bytes_per_run=int(o.get("max_new_bytes_per_run", 1_500_000_000)),
            )
        if spec.connector == "uis_country":
            return uis_country_resource(
                geo_unit=str(o.get("geo_unit", "CIV")),
                start_year=o.get("start_year"),
                end_year=o.get("end_year"),
                user_agent=self.settings.user_agent,
                snapshot_dir=p["raw"],
                upstream_state_path=upstream_state_path,
            )
        if spec.connector == "geoboundaries":
            return geoboundaries_resource(
                api_url=spec.source_url,
                source_id=spec.source_id,
                user_agent=self.settings.user_agent,
                snapshot_dir=p["raw"],
                upstream_state_path=upstream_state_path,
            )
        if spec.connector == "ilostat_ref_area":
            return ilostat_ref_area_resource(
                country=str(o.get("country", "CIV")),
                frequencies=o.get("frequencies", []),
                base_url=str(o.get("base_url", ILOSTAT_DATA_API)),
                user_agent=self.settings.user_agent,
                snapshot_dir=p["raw"],
                upstream_state_path=upstream_state_path,
                request_pause_seconds=float(o.get("request_pause_seconds", 0.05)),
            )
        if spec.connector == "osm_geofabrik":
            return geofabrik_snapshot_resource(
                page_url=spec.source_url,
                output_dir=p["raw"],
                source_id=spec.source_id,
                format=str(o.get("format", "pbf")),
                user_agent=self.settings.user_agent,
                upstream_state_path=upstream_state_path,
            )
        if spec.connector == "bulk_catalog":
            return bulk_catalog_resource(
                source_id=spec.source_id,
                page_url=spec.source_url,
                user_agent=self.settings.user_agent,
                download_dir=p["raw"],
                download_patterns=list(o.get("download_patterns", [])),
                max_downloads=int(o.get("max_downloads", 0)),
                max_bytes=int(o.get("max_bytes", 250_000_000)),
            )
        if spec.connector == "official_docs":
            dev_meta = dict(meta)
            dev_meta.update({
                "country_code": "GLOBAL",
                "country_name": "Global",
                "geographic_scope": "GLOBAL",
                "primary_domain": "software_development",
                "language": str(o.get("language") or "en"),
                "document_type": "DEVELOPER_DOCUMENTATION",
                "corpus_scope": str(o.get("corpus_scope") or "PROGRAMMING_DOCUMENTATION"),
                "programming_language": str(o.get("programming_language") or "General"),
                "version_policy": str(o.get("version_policy") or "CURRENT_STABLE"),
                "training_eligible": bool(o.get("training_eligible", False)),
                "license_review_status": str(o.get("license_review_status") or "UNREVIEWED"),
            })
            for key in (
                "framework", "runtime", "library", "tool", "ecosystem", "doc_version",
                "license_name", "license_url", "source_strategy", "public_docs_url",
                "version_repository", "version_ref_strategy", "canonical_repository",
            ):
                if o.get(key) is not None:
                    dev_meta[key] = o.get(key)
            return official_docs_resource(
                source_id=spec.source_id,
                url=spec.source_url,
                user_agent=self.settings.user_agent,
                discovery_urls=list(o.get("discovery_urls", [])),
                include_prefixes=list(o.get("include_prefixes", [])),
                exclude_patterns=list(o.get("exclude_patterns", [])),
                max_pages=int(o.get("max_pages", 100_000)),
                max_sitemaps=int(o.get("max_sitemaps", 1_000)),
                max_bytes_per_page=int(o.get("max_bytes_per_page", 12_000_000)),
                max_new_bytes_per_run=int(o.get("max_new_bytes_per_run", 500_000_000)),
                request_pause_seconds=float(o.get("request_pause_seconds", 0.02)),
                allow_crawl_fallback=bool(o.get("allow_crawl_fallback", True)),
                snapshot_dir=p["raw"],
                metadata_base=dev_meta,
                upstream_state_path=upstream_state_path,
                license_name=o.get("license_name"),
                license_url=o.get("license_url"),
                training_eligible=bool(o.get("training_eligible", False)),
                license_review_status=str(o.get("license_review_status") or "UNREVIEWED"),
            )
        if spec.connector == "public_web":
            return public_document_resource(
                source_id=spec.source_id,
                url=spec.source_url,
                force=force,
                user_agent=self.settings.user_agent,
                crawl=bool(o.get("crawl", False)),
                max_pages=int(o.get("max_pages", 1)),
                max_bytes=int(o.get("max_bytes", 20_000_000)),
                metadata_only=bool(o.get("metadata_only", False)),
                snapshot_dir=p["documents"],
                verify_ssl=bool(o.get("verify_ssl", True)),
                metadata_base=meta,
                upstream_state_path=upstream_state_path,
            )
        raise ValueError(f"unsupported connector {spec.connector!r} for {spec.source_id}")

    def _catalog(self) -> None:
        rebuild_catalog(self.settings, self.registry.list())

    def _write_manifest(self, spec: SourceSpec, *, status: str, started: str, finished: str, details: str) -> None:
        state = self.freshness.data.get(spec.source_id, {})
        manifest = write_source_manifest(
            self.settings,
            spec,
            status=status,
            connector=spec.connector,
            started_at=started,
            finished_at=finished,
            details=details,
            freshness_state=state,
            due=self.freshness.due(spec),
        )
        if _is_programming_docs(spec):
            options = spec.options
            taxonomy = {
                "corpus_scope": str(options.get("corpus_scope") or "PROGRAMMING_DOCUMENTATION"),
                "programming_language": str(options.get("programming_language") or "General"),
                "framework": options.get("framework"),
                "runtime": options.get("runtime"),
                "library": options.get("library"),
                "tool": options.get("tool"),
                "doc_version": options.get("doc_version"),
                "version_policy": str(options.get("version_policy") or "CURRENT_STABLE"),
                "license_name": options.get("license_name"),
                "license_url": options.get("license_url"),
                "license_review_status": str(options.get("license_review_status") or "UNREVIEWED"),
                "training_eligible": bool(options.get("training_eligible", False)),
            }
            manifest["country_code"] = "GLOBAL"
            manifest["country_name"] = "Global"
            metadata = manifest.setdefault("metadata", {})
            metadata.update({
                "country_code": "GLOBAL",
                "country_name": "Global",
                "geographic_scope": "GLOBAL",
                "primary_domain": "software_development",
                "language": str(options.get("language") or "en"),
                "document_type": "DEVELOPER_DOCUMENTATION",
                **taxonomy,
            })
            manifest["programming_documentation"] = taxonomy
            atomic_write_json(source_paths(self.settings, spec)["manifest"], manifest)

    def _post_sync_cleanup(self, spec: SourceSpec) -> None:
        if spec.source_id != "civ_ilostat":
            return
        paths = source_paths(self.settings, spec)
        stats = load_json(paths["raw"] / "ilostat_sync_stats.json", {})
        if not isinstance(stats, dict):
            return
        selected = int(stats.get("selected_indicators") or 0)
        accounted = int(stats.get("unchanged") or 0) + int(stats.get("with_country_rows") or 0) + int(stats.get("without_country_rows") or 0)
        if selected <= 0 or int(stats.get("failed") or 0) != 0 or accounted < selected:
            return
        legacy = paths["tables"] / "data" / "ilostat_civ"
        if not legacy.exists():
            return
        archive = paths["raw"] / "legacy"
        archive.mkdir(parents=True, exist_ok=True)
        target = archive / f"ilostat_civ-v081-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        shutil.move(str(legacy), str(target))

    def sync(self, source_id: str, *, force: bool = False) -> SyncResult:
        spec = self.registry.get(source_id)
        if not spec.enabled:
            raise PermissionError(f"{source_id} is disabled — enable it first with: ivoiredata source enable {source_id}")
        if not spec.public:
            raise PermissionError(f"{source_id} is not configured for unattended public ingestion")
        started = _now()
        try:
            pipeline = get_source_pipeline(self.settings, spec)
            details = str(pipeline.run(self._resource_for(spec, force=force), loader_file_format="parquet"))
            self._post_sync_cleanup(spec)
            finished = _now()
            self.freshness.mark(source_id, success=True, details=details)
            self._write_manifest(spec, status="success", started=started, finished=finished, details=details)
            self._catalog()
            return SyncResult(source_id, "success", started, finished, spec.connector, details)
        except Exception as exc:
            finished = _now()
            details = str(exc)
            self.freshness.mark(source_id, success=False, details=details)
            self._write_manifest(spec, status="error", started=started, finished=finished, details=details)
            self._catalog()
            return SyncResult(source_id, "error", started, finished, spec.connector, details)

    def sync_due(self, *, force: bool = False) -> list[SyncResult]:
        results: list[SyncResult] = []
        for spec in self.registry.list(public_only=True, auto_only=True):
            if force or self.freshness.due(spec):
                results.append(self.sync(spec.source_id, force=force))
        return results

    def sync_all(self, *, force: bool = False) -> list[SyncResult]:
        results: list[SyncResult] = []
        for spec in self.registry.list(public_only=True):
            if force or self.freshness.due(spec):
                results.append(self.sync(spec.source_id, force=force))
        return results

    def audit(self, *, public_only: bool = True) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for spec in self.registry.list(public_only=public_only):
            paths = source_paths(self.settings, spec)
            manifest = load_json(paths["manifest"], {})
            state = self.freshness.data.get(spec.source_id, {})
            delivery = manifest.get("delivery", {}) if isinstance(manifest, dict) else {}
            metadata = manifest.get("metadata", {}) if isinstance(manifest, dict) else {}
            rows.append({
                "source_id": spec.source_id,
                "connector": spec.connector,
                "sync_status": str(manifest.get("sync_status") or manifest.get("status") or state.get("last_status") or "NEVER").upper(),
                "delivery_status": str(delivery.get("status") or "UNKNOWN").upper(),
                "freshness_status": "DUE" if self.freshness.due(spec) else "FRESH",
                "transport": str(metadata.get("transport_security") or metadata.get("transport") or "UNKNOWN").upper(),
                "structured_rows": int(delivery.get("rows", {}).get("structured") or 0) if isinstance(delivery.get("rows"), dict) else 0,
                "document_rows": int(delivery.get("rows", {}).get("documents") or 0) if isinstance(delivery.get("rows"), dict) else 0,
                "total_rows": int(delivery.get("rows", {}).get("total") or 0) if isinstance(delivery.get("rows"), dict) else 0,
            })
        return {"summary": _audit_summary(rows), "rows": rows}

    def upstream_audit(self, source_id: str | None = None) -> dict[str, Any]:
        return self.upstreams.audit(source_id)

    def coverage_audit(self, *, public_only: bool = True) -> dict[str, Any]:
        from .coverage import coverage_audit
        return coverage_audit(self._ci_gold_view(), public_only=public_only)

    def quality_audit(self, *, public_only: bool = True) -> dict[str, Any]:
        from .quality import quality_audit
        return quality_audit(self._ci_gold_view(), public_only=public_only)

    def _ci_gold_view(self) -> _CIGoldEngineView:
        return _CIGoldEngineView(self)

    def ci_gold(self) -> dict[str, Any]:
        from .ci_gold import ci_gold
        return ci_gold(self._ci_gold_view())

    def write_ci_gold(self) -> dict[str, Any]:
        from .ci_gold import write_ci_gold
        return write_ci_gold(self._ci_gold_view())

    def programming_docs_audit(self, *, language: str | None = None) -> dict[str, Any]:
        from .programming_docs import programming_docs_audit
        return programming_docs_audit(self, language=language)

    def sync_programming_docs(self, *, language: str | None = None, force: bool = False) -> list[SyncResult]:
        from .programming_docs import sync_programming_docs
        return sync_programming_docs(self, language=language, force=force)

    def write_programming_docs_report(self, *, language: str | None = None) -> dict[str, Any]:
        from .programming_docs import write_programming_docs_report
        return write_programming_docs_report(self, language=language)
