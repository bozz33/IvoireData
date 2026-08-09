from __future__ import annotations

from datetime import datetime, timezone

from .connectors.bulk_catalog import bulk_catalog_resource
from .connectors.data_gouv_ci import data_gouv_ci_resource, dataset_id_from_public_url
from .connectors.geoboundaries import geoboundaries_resource
from .connectors.http_file import http_file_resource
from .connectors.ilostat import ilostat_ref_area_resource
from .connectors.osm_geofabrik import geofabrik_snapshot_resource
from .connectors.public_web import public_document_resource
from .connectors.world_bank import world_bank_wdi_resource
from .delivery import ensure_source_layout, rebuild_catalog, write_source_manifest
from .freshness import FreshnessStore
from .models import SourceSpec, SyncResult
from .pipeline import get_source_pipeline
from .registry import SourceRegistry
from .settings import Settings


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class IvoireDataEngine:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.registry = SourceRegistry.load(self.settings.registry_path, self.settings.runtime_config_path)
        self.freshness = FreshnessStore(self.settings.state_dir / "freshness.json")

    def _resource_for(self, spec: SourceSpec, *, force: bool = False):
        p = ensure_source_layout(self.settings, spec)
        o = spec.options
        if spec.connector == "data_gouv_ci":
            dsid = None if spec.source_id == "civ_datagouv_catalog" else dataset_id_from_public_url(spec.source_url)
            return data_gouv_ci_resource(dataset_ids=[dsid] if dsid else None, force=force, user_agent=self.settings.user_agent, limit=o.get("limit"), snapshot_dir=p["raw"])
        if spec.connector == "http_file":
            return http_file_resource(source_id=spec.source_id, url=spec.source_url, force=force, user_agent=self.settings.user_agent, snapshot_dir=p["raw"])
        if spec.connector == "world_bank_wdi":
            return world_bank_wdi_resource(country=str(o.get("country", "CIV")), source=int(o.get("source", 2)), indicator_limit=o.get("indicator_limit"), batch_size=int(o.get("batch_size", 60)), user_agent=self.settings.user_agent, snapshot_dir=p["raw"])
        if spec.connector == "geoboundaries":
            return geoboundaries_resource(api_url=spec.source_url, source_id=spec.source_id, user_agent=self.settings.user_agent)
        if spec.connector == "ilostat_ref_area":
            return ilostat_ref_area_resource(country=str(o.get("country", "CIV")), frequencies=o.get("frequencies", ["A"]), base_url=str(o.get("base_url", "https://rplumber.ilo.org/files/ref_area")), user_agent=self.settings.user_agent, snapshot_dir=p["raw"])
        if spec.connector == "osm_geofabrik":
            return geofabrik_snapshot_resource(page_url=spec.source_url, output_dir=p["raw"], source_id=spec.source_id, format=str(o.get("format", "pbf")), user_agent=self.settings.user_agent)
        if spec.connector == "bulk_catalog":
            return bulk_catalog_resource(source_id=spec.source_id, page_url=spec.source_url, user_agent=self.settings.user_agent, download_dir=p["raw"], download_patterns=list(o.get("download_patterns", [])), max_downloads=int(o.get("max_downloads", 0)), max_bytes=int(o.get("max_bytes", 250_000_000)))
        if spec.connector == "public_web":
            return public_document_resource(source_id=spec.source_id, url=spec.source_url, force=force, user_agent=self.settings.user_agent, crawl=bool(o.get("crawl", False)), max_pages=int(o.get("max_pages", 1)), max_bytes=int(o.get("max_bytes", 20_000_000)), metadata_only=bool(o.get("metadata_only", False)), snapshot_dir=p["documents"], verify_ssl=bool(o.get("verify_ssl", True)))
        raise ValueError(f"unsupported connector {spec.connector!r} for {spec.source_id}")

    def _catalog(self) -> None:
        rebuild_catalog(self.settings, self.registry.list())

    def sync(self, source_id: str, *, force: bool = False) -> SyncResult:
        spec = self.registry.get(source_id)
        if not spec.public:
            raise PermissionError(f"{source_id} is not configured for unattended public ingestion")
        started = _now()
        try:
            pipeline = get_source_pipeline(self.settings, spec)
            details = str(pipeline.run(self._resource_for(spec, force=force), loader_file_format="parquet"))
            finished = _now()
            self.freshness.mark(source_id, success=True, details=details)
            write_source_manifest(self.settings, spec, status="success", connector=spec.connector, started_at=started, finished_at=finished, details=details)
            self._catalog()
            return SyncResult(source_id, "success", started, finished, spec.connector, details)
        except Exception as exc:
            finished = _now(); details = str(exc)
            self.freshness.mark(source_id, success=False, details=details)
            write_source_manifest(self.settings, spec, status="error", connector=spec.connector, started_at=started, finished_at=finished, details=details)
            self._catalog()
            return SyncResult(source_id, "error", started, finished, spec.connector, details)

    def sync_due(self, *, auto_only: bool = True, public_only: bool = True, force: bool = False) -> list[SyncResult]:
        return [self.sync(s.source_id, force=force) for s in self.registry.list(public_only=public_only, auto_only=auto_only) if force or self.freshness.due(s)]

    def coverage(self) -> dict:
        specs = self.registry.list(); public = [s for s in specs if s.public]; automatic = [s for s in public if s.auto_sync]
        by_connector: dict[str, int] = {}; by_domain: dict[str, int] = {}
        for spec in automatic:
            by_connector[spec.connector] = by_connector.get(spec.connector, 0) + 1
            by_domain[spec.domain] = by_domain.get(spec.domain, 0) + 1
        return {"sources_total": len(specs), "sources_public_syncable": len(public), "sources_auto_sync": len(automatic), "sources_manual_or_controlled": len(specs) - len(public), "auto_by_connector": dict(sorted(by_connector.items())), "auto_by_domain": dict(sorted(by_domain.items()))}
