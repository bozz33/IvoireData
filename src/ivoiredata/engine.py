from __future__ import annotations

import json
from datetime import datetime, timezone

from .connectors.bulk_catalog import bulk_catalog_resource
from .connectors.data_gouv_ci import data_gouv_ci_resource, dataset_id_from_public_url
from .connectors.faostat import DEFAULT_DATASETS as FAOSTAT_DEFAULT_DATASETS, faostat_country_resource
from .connectors.geoboundaries import geoboundaries_resource
from .connectors.http_file import http_file_resource
from .connectors.ilostat import ilostat_ref_area_resource
from .connectors.osm_geofabrik import geofabrik_snapshot_resource
from .connectors.public_web import public_document_resource
from .connectors.uis import uis_country_resource
from .connectors.world_bank import world_bank_wdi_resource
from .connectors.world_bank_projects import world_bank_projects_resource
from .delivery import ensure_source_layout, rebuild_catalog, source_paths, write_source_manifest
from .freshness import FreshnessStore
from .models import SourceSpec, SyncResult
from .pipeline import get_source_pipeline
from .registry import SourceRegistry
from .runtime_control import RuntimeControl
from .settings import Settings


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class IvoireDataEngine:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.runtime = RuntimeControl(self.settings)
        self.registry = SourceRegistry.load(
            self.settings.registry_path,
            self.settings.runtime_config_path,
            self.settings.runtime_overrides_path,
        )
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
        if spec.connector == "world_bank_projects":
            return world_bank_projects_resource(country_code=str(o.get("country_code", "CI")), page_size=int(o.get("page_size", 50)), user_agent=self.settings.user_agent, snapshot_dir=p["raw"])
        if spec.connector == "faostat_country":
            return faostat_country_resource(
                country=str(o.get("country", "CIV")),
                aliases=o.get("aliases", ["Côte d'Ivoire", "Cote d'Ivoire", "Côte d’Ivoire", "Ivory Coast"]),
                datasets=o.get("datasets") or FAOSTAT_DEFAULT_DATASETS,
                user_agent=self.settings.user_agent,
                snapshot_dir=p["raw"],
                max_bytes_per_file=int(o.get("max_bytes_per_file", 400_000_000)),
            )
        if spec.connector == "uis_country":
            return uis_country_resource(
                geo_unit=str(o.get("geo_unit", "CIV")),
                start_year=o.get("start_year"),
                end_year=o.get("end_year"),
                user_agent=self.settings.user_agent,
                snapshot_dir=p["raw"],
            )
        if spec.connector == "geoboundaries":
            return geoboundaries_resource(api_url=spec.source_url, source_id=spec.source_id, user_agent=self.settings.user_agent)
        if spec.connector == "ilostat_ref_area":
            return ilostat_ref_area_resource(country=str(o.get("country", "CIV")), frequencies=o.get("frequencies", []), base_url=str(o.get("base_url", "https://rplumber.ilo.org/data/indicator")), user_agent=self.settings.user_agent, snapshot_dir=p["raw"])
        if spec.connector == "osm_geofabrik":
            return geofabrik_snapshot_resource(page_url=spec.source_url, output_dir=p["raw"], source_id=spec.source_id, format=str(o.get("format", "pbf")), user_agent=self.settings.user_agent)
        if spec.connector == "bulk_catalog":
            return bulk_catalog_resource(source_id=spec.source_id, page_url=spec.source_url, user_agent=self.settings.user_agent, download_dir=p["raw"], download_patterns=list(o.get("download_patterns", [])), max_downloads=int(o.get("max_downloads", 0)), max_bytes=int(o.get("max_bytes", 250_000_000)))
        if spec.connector == "public_web":
            return public_document_resource(source_id=spec.source_id, url=spec.source_url, force=force, user_agent=self.settings.user_agent, crawl=bool(o.get("crawl", False)), max_pages=int(o.get("max_pages", 1)), max_bytes=int(o.get("max_bytes", 20_000_000)), metadata_only=bool(o.get("metadata_only", False)), snapshot_dir=p["documents"], verify_ssl=bool(o.get("verify_ssl", True)))
        raise ValueError(f"unsupported connector {spec.connector!r} for {spec.source_id}")

    def _catalog(self) -> None:
        rebuild_catalog(self.settings, self.registry.list())

    def _write_manifest(self, spec: SourceSpec, *, status: str, started: str, finished: str, details: str) -> None:
        state = self.freshness.data.get(spec.source_id, {})
        write_source_manifest(
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

    def sync_due(self, *, auto_only: bool = True, public_only: bool = True, force: bool = False) -> list[SyncResult]:
        return [self.sync(s.source_id, force=force) for s in self.registry.list(public_only=public_only, auto_only=auto_only) if force or self.freshness.due(s)]

    def coverage(self) -> dict:
        all_specs = self.registry.all()
        specs = self.registry.list()
        public = [s for s in specs if s.public]
        automatic = [s for s in public if s.auto_sync]
        by_connector: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        for spec in automatic:
            by_connector[spec.connector] = by_connector.get(spec.connector, 0) + 1
            by_domain[spec.domain] = by_domain.get(spec.domain, 0) + 1
        return {
            "sources_registry": len(all_specs),
            "sources_enabled": len(specs),
            "sources_disabled": sum(1 for s in all_specs if not s.enabled),
            "sources_public_syncable": len(public),
            "sources_auto_sync": len(automatic),
            "automatic_updates_enabled": self.runtime.automatic_enabled,
            "scheduler_interval_seconds": self.runtime.scheduler_interval_seconds,
            "sources_manual_or_controlled": len(specs) - len(automatic),
            "auto_by_connector": dict(sorted(by_connector.items())),
            "auto_by_domain": dict(sorted(by_domain.items())),
        }

    def audit(self, *, public_only: bool = True) -> dict:
        rows = []
        delivery_counts: dict[str, int] = {}
        sync_counts: dict[str, int] = {}
        freshness_counts: dict[str, int] = {}
        for spec in self.registry.list(public_only=public_only):
            manifest_path = source_paths(self.settings, spec)["manifest"]
            state = self.freshness.data.get(spec.source_id, {})
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    manifest = {}
            else:
                manifest = {}
            delivery = manifest.get("delivery", {})
            sync_status = str(manifest.get("status") or state.get("last_status") or "never").upper()
            delivery_status = str(manifest.get("delivery_status") or delivery.get("status") or "EMPTY")
            if state.get("last_status") == "error" and state.get("last_success"):
                freshness_status = "STALE"
            elif not state.get("last_success"):
                freshness_status = "NEVER_SYNCED"
            else:
                freshness_status = "DUE" if self.freshness.due(spec) else "FRESH"
            row = {
                "source_id": spec.source_id,
                "domain": spec.domain,
                "connector": spec.connector,
                "sync_status": sync_status,
                "delivery_status": delivery_status,
                "freshness_status": freshness_status,
                "transport_security": manifest.get("transport_security") or manifest.get("transport", {}).get("security"),
                "rows": int(delivery.get("rows") or manifest.get("inventory", {}).get("tables", {}).get("rows") or 0),
                "raw_files": int(delivery.get("raw_files") or manifest.get("inventory", {}).get("raw", {}).get("files") or 0),
                "table_files": int(delivery.get("table_files") or manifest.get("inventory", {}).get("tables", {}).get("files") or 0),
                "document_files": int(delivery.get("document_files") or manifest.get("inventory", {}).get("documents", {}).get("files") or 0),
                "warnings": manifest.get("warnings", []),
                "last_success": state.get("last_success"),
            }
            rows.append(row)
            sync_counts[sync_status] = sync_counts.get(sync_status, 0) + 1
            delivery_counts[delivery_status] = delivery_counts.get(delivery_status, 0) + 1
            freshness_counts[freshness_status] = freshness_counts.get(freshness_status, 0) + 1

        usable = sum(1 for row in rows if row["delivery_status"] != "EMPTY")
        return {
            "summary": {
                "sources": len(rows),
                "usable_delivery": usable,
                "empty_delivery": len(rows) - usable,
                "sync": dict(sorted(sync_counts.items())),
                "delivery": dict(sorted(delivery_counts.items())),
                "freshness": dict(sorted(freshness_counts.items())),
            },
            "rows": rows,
        }
