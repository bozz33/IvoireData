from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone

from .connectors.bulk_catalog import bulk_catalog_resource
from .connectors.data_gouv_ci import data_gouv_ci_resource, dataset_id_from_public_url
from .connectors.faostat import CATALOG_URL as FAOSTAT_CATALOG_URL, faostat_country_resource
from .connectors.geoboundaries import geoboundaries_resource
from .connectors.http_file import http_file_resource
from .connectors.ilostat import DATA_API as ILOSTAT_DATA_API, ilostat_ref_area_resource
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
from .state_io import load_json
from .upstream_state import UpstreamState


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
            [self.settings.ci_gold_runtime_path],
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

    def _post_sync_cleanup(self, spec: SourceSpec) -> None:
        """Move obsolete legacy tables only after their replacement sync is complete."""
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

    def sync_due(self, *, auto_only: bool = True, public_only: bool = True, force: bool = False) -> list[SyncResult]:
        return [self.sync(s.source_id, force=force) for s in self.registry.list(public_only=public_only, auto_only=auto_only) if force or self.freshness.due(s)]

    def upstream_audit(self, source_id: str | None = None) -> dict:
        """Summarize the persistent network cache and last upstream result by source."""
        if source_id is not None:
            self.registry.get(source_id)
            source_ids = [source_id]
        else:
            source_ids = [spec.source_id for spec in self.registry.all()]
        rows = []
        totals: dict[str, int] = {}
        for sid in source_ids:
            artifacts = self.upstreams.source_rows(sid)
            result_counts: dict[str, int] = {}
            methods: dict[str, int] = {}
            downloaded_bytes = 0
            last_checked = None
            last_downloaded = None
            errors = []
            for item in artifacts:
                result = str(item.get("last_result") or "UNKNOWN")
                result_counts[result] = result_counts.get(result, 0) + 1
                totals[result] = totals.get(result, 0) + 1
                method = str(item.get("method") or "")
                if method:
                    methods[method] = methods.get(method, 0) + 1
                if item.get("last_result") == "DOWNLOADED":
                    downloaded_bytes += int(item.get("size_bytes") or 0)
                checked = item.get("last_checked")
                downloaded = item.get("last_downloaded")
                if checked and (last_checked is None or str(checked) > str(last_checked)):
                    last_checked = checked
                if downloaded and (last_downloaded is None or str(downloaded) > str(last_downloaded)):
                    last_downloaded = downloaded
                if item.get("error"):
                    errors.append({"artifact_id": item.get("artifact_id"), "error": item.get("error"), "http_status": item.get("http_status")})
            rows.append({
                "source_id": sid,
                "artifacts": len(artifacts),
                "last_results": dict(sorted(result_counts.items())),
                "methods": dict(sorted(methods.items())),
                "cached_bytes": downloaded_bytes,
                "last_checked": last_checked,
                "last_downloaded": last_downloaded,
                "errors": errors[:50],
            })
        return {
            "state_path": str(self.settings.upstream_state_path),
            "summary": {"sources": len(rows), "artifacts": sum(row["artifacts"] for row in rows), "last_results": dict(sorted(totals.items()))},
            "rows": rows,
        }

    def qualification_baseline(self) -> list[str]:
        """Return AUTO sources that are clean and fresh at qualification start."""
        audit_map = {row["source_id"]: row for row in self.audit(public_only=True)["rows"]}
        baseline: list[str] = []
        for spec in self.registry.list(public_only=True, auto_only=True):
            row = audit_map.get(spec.source_id, {})
            if (
                row.get("sync_status") == "SUCCESS"
                and row.get("delivery_status") != "EMPTY"
                and row.get("freshness_status") == "FRESH"
            ):
                baseline.append(spec.source_id)
        return sorted(baseline)

    def start_qualification(self) -> dict:
        return self.qualification.start(baseline_sources=self.qualification_baseline())

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
            "country_code": "CIV",
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
        transport_counts: dict[str, int] = {}
        structured_rows = 0
        document_rows = 0
        total_rows = 0
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
            sync_status = str(manifest.get("status") or state.get("last_status") or "NEVER").upper()
            delivery_status = str(delivery.get("status") or "EMPTY").upper()
            fresh = self.freshness.status(spec)
            freshness_status = str(fresh.get("status") or "UNKNOWN").upper()
            transfer = str(delivery.get("transport") or "UNKNOWN").upper()
            structured = int(delivery.get("structured_rows") or 0)
            documents = int(delivery.get("document_rows") or 0)
            rows_count = int(delivery.get("total_rows") or structured + documents)
            structured_rows += structured
            document_rows += documents
            total_rows += rows_count
            delivery_counts[delivery_status] = delivery_counts.get(delivery_status, 0) + 1
            sync_counts[sync_status] = sync_counts.get(sync_status, 0) + 1
            freshness_counts[freshness_status] = freshness_counts.get(freshness_status, 0) + 1
            transport_counts[transfer] = transport_counts.get(transfer, 0) + 1
            rows.append({
                "source_id": spec.source_id,
                "title": spec.title,
                "provider": spec.provider,
                "domain": spec.domain,
                "priority": spec.priority,
                "connector": spec.connector,
                "refresh_hours": spec.refresh_hours,
                "auto_sync": spec.auto_sync,
                "sync_status": sync_status,
                "delivery_status": delivery_status,
                "freshness_status": freshness_status,
                "transport": transfer,
                "structured_rows": structured,
                "document_rows": documents,
                "total_rows": rows_count,
                "last_success": state.get("last_success"),
                "last_attempt": state.get("last_attempt"),
                "last_error": state.get("last_error"),
            })
        return {
            "summary": {
                "sources": len(rows),
                "delivery": dict(sorted(delivery_counts.items())),
                "sync": dict(sorted(sync_counts.items())),
                "freshness": dict(sorted(freshness_counts.items())),
                "transport": dict(sorted(transport_counts.items())),
                "structured_rows": structured_rows,
                "document_rows": document_rows,
                "total_rows": total_rows,
            },
            "rows": rows,
        }
