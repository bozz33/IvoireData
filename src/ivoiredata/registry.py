from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from .models import SourceSpec

def infer_connector(spec: SourceSpec) -> str:
    host = (urlparse(spec.source_url).hostname or "").lower()
    path = urlparse(spec.source_url).path.lower()
    if spec.source_id == "civ_datagouv_catalog":
        return "data_gouv_ci"
    if host.endswith("data.gouv.ci") and "/datasets/" in path:
        return "data_gouv_ci"
    if path.endswith((".csv", ".json", ".jsonl", ".parquet", ".xml", ".xlsx", ".xls")):
        return "http_file"
    return "public_web"

class SourceRegistry:
    def __init__(self, sources: dict[str, SourceSpec]):
        self._sources = sources

    @classmethod
    def load(cls, csv_path: Path, runtime_path: Path | None = None) -> "SourceRegistry":
        sources: dict[str, SourceSpec] = {}
        with csv_path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                spec = SourceSpec(source_id=row["source_id"], title=row["title"], domain=row["domain"], provider=row["provider"], source_url=row["source_url"], rights_tier=row["rights_tier"], access_tier=row["access_tier"], priority=row["priority"])
                sources[spec.source_id] = spec
        config: dict = {}
        if runtime_path and runtime_path.exists():
            config = json.loads(runtime_path.read_text(encoding="utf-8"))
        defaults = config.get("defaults", {})
        overrides = config.get("sources", {})
        for sid, spec in list(sources.items()):
            override = overrides.get(sid, {})
            refresh = int(override.get("refresh_hours", defaults.get("refresh_hours", 168)))
            connector = override.get("connector", defaults.get("connector", "auto"))
            auto_sync = bool(override.get("auto_sync", defaults.get("auto_sync", False)))
            enabled = bool(override.get("enabled", True))
            options = dict(defaults.get("options", {})); options.update(override.get("options", {}))
            spec = replace(spec, connector=connector, refresh_hours=refresh, auto_sync=auto_sync, enabled=enabled, options=options)
            if spec.connector == "auto":
                spec = replace(spec, connector=infer_connector(spec))
            sources[sid] = spec
        return cls(sources)

    def get(self, source_id: str) -> SourceSpec:
        try: return self._sources[source_id]
        except KeyError as exc: raise KeyError(f"unknown source_id: {source_id}") from exc

    def list(self, *, public_only: bool = False, auto_only: bool = False) -> list[SourceSpec]:
        items = [s for s in self._sources.values() if s.enabled]
        if public_only: items = [s for s in items if s.public]
        if auto_only: items = [s for s in items if s.auto_sync]
        return sorted(items, key=lambda s: (s.priority, s.domain, s.source_id))
