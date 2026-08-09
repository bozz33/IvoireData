from __future__ import annotations

from datetime import datetime, timezone

from .connectors.data_gouv_ci import data_gouv_ci_resource, dataset_id_from_public_url
from .connectors.http_file import http_file_resource
from .connectors.public_web import public_document_resource
from .connectors.world_bank import world_bank_wdi_resource
from .connectors.geoboundaries import geoboundaries_resource
from .freshness import FreshnessStore
from .models import SourceSpec, SyncResult
from .pipeline import get_pipeline
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
        if spec.connector == "data_gouv_ci":
            dsid = None if spec.source_id == "civ_datagouv_catalog" else dataset_id_from_public_url(spec.source_url)
            return data_gouv_ci_resource(dataset_ids=[dsid] if dsid else None, force=force, user_agent=self.settings.user_agent, limit=spec.options.get("limit"))
        if spec.connector == "http_file":
            return http_file_resource(source_id=spec.source_id, url=spec.source_url, force=force, user_agent=self.settings.user_agent)
        if spec.connector == "world_bank_wdi":
            return world_bank_wdi_resource(country=str(spec.options.get("country", "CIV")), source=int(spec.options.get("source", 2)), indicator_limit=spec.options.get("indicator_limit"), batch_size=int(spec.options.get("batch_size", 60)), user_agent=self.settings.user_agent)
        if spec.connector == "geoboundaries":
            return geoboundaries_resource(api_url=spec.source_url, source_id=spec.source_id, user_agent=self.settings.user_agent)
        if spec.connector == "public_web":
            return public_document_resource(source_id=spec.source_id, url=spec.source_url, force=force, user_agent=self.settings.user_agent, crawl=bool(spec.options.get("crawl", False)), max_pages=int(spec.options.get("max_pages", 1)), max_bytes=int(spec.options.get("max_bytes", 20_000_000)))
        raise ValueError(f"unsupported connector {spec.connector!r} for {spec.source_id}")

    def sync(self, source_id: str, *, force: bool = False) -> SyncResult:
        spec = self.registry.get(source_id)
        if not spec.public:
            raise PermissionError(f"{source_id} is not configured for unattended public ingestion")
        started = _now()
        try:
            info = get_pipeline(self.settings).run(self._resource_for(spec, force=force)); details = str(info)
            self.freshness.mark(source_id, success=True, details=details)
            return SyncResult(source_id, "success", started, _now(), spec.connector, details)
        except Exception as exc:
            self.freshness.mark(source_id, success=False, details=str(exc))
            return SyncResult(source_id, "error", started, _now(), spec.connector, str(exc))

    def sync_due(self, *, auto_only: bool = True, public_only: bool = True, force: bool = False) -> list[SyncResult]:
        results = []
        for spec in self.registry.list(public_only=public_only, auto_only=auto_only):
            if force or self.freshness.due(spec):
                results.append(self.sync(spec.source_id, force=force))
        return results

    def coverage(self) -> dict:
        specs = self.registry.list()
        public = [s for s in specs if s.public]
        automatic = [s for s in public if s.auto_sync]
        by_connector: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        for spec in automatic:
            by_connector[spec.connector] = by_connector.get(spec.connector, 0) + 1
            by_domain[spec.domain] = by_domain.get(spec.domain, 0) + 1
        return {"sources_total": len(specs), "sources_public_syncable": len(public), "sources_auto_sync": len(automatic), "sources_manual_or_controlled": len(specs) - len(public), "auto_by_connector": dict(sorted(by_connector.items())), "auto_by_domain": dict(sorted(by_domain.items()))}
