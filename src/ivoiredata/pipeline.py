from __future__ import annotations

import re
from typing import Any

from .delivery import ensure_source_layout
from .dlt_tables import make_incremental_replace_safe
from .models import SourceSpec
from .settings import Settings

_SAFE = re.compile(r"[^a-zA-Z0-9_]+")
_INCREMENTAL_PARTIAL_CONNECTORS = {
    "data_gouv_ci",
    "world_bank_wdi",
    "world_bank_projects",
    "faostat_country",
    "uis_country",
    "geoboundaries",
    "ilostat_ref_area",
    "http_file",
}


def _pipeline_name(base: str, source_id: str) -> str:
    suffix = _SAFE.sub("_", source_id).strip("_")
    return f"{base}_{suffix}"[:120]


class _SafePipelineProxy:
    """Delegate to dlt while isolating replace semantics per emitted table."""

    def __init__(self, pipeline: Any, *, protect_partial_replace: bool):
        self._pipeline = pipeline
        self._protect_partial_replace = protect_partial_replace

    def __getattr__(self, name: str):
        return getattr(self._pipeline, name)

    def run(self, data: Any, *args: Any, **kwargs: Any):
        if self._protect_partial_replace and hasattr(data, "apply_hints") and hasattr(data, "add_map"):
            data = make_incremental_replace_safe(data)
        return self._pipeline.run(data, *args, **kwargs)


def get_pipeline(settings: Settings):
    """Legacy/global filesystem pipeline kept for backwards-compatible SQL access."""
    import dlt

    settings.configure_dlt_env()
    return dlt.pipeline(
        pipeline_name=settings.pipeline_name,
        destination="filesystem",
        dataset_name=settings.dataset_name,
    )


def get_source_pipeline(settings: Settings, spec: SourceSpec):
    """Create an isolated local dlt pipeline for one source.

    Physical output is stored below:
      data_lake/domains/<domain>/<source_id>/tables/data/<table>/...

    Each source gets its own dlt state, schema and load history so a failing or
    schema-changing upstream cannot corrupt another domain/source package.

    Incremental structured connectors may emit only changed tables. For those
    connectors the proxy rewrites resource-level replace semantics into independent
    per-table variants, preventing an unchanged omitted table from being truncated.
    """
    import dlt
    from dlt.destinations import filesystem

    paths = ensure_source_layout(settings, spec)
    destination = filesystem(
        bucket_url=paths["tables"].resolve().as_uri(),
        layout="{table_name}/{load_package_timestamp}/{load_id}.{file_id}.{ext}",
        kwargs={"auto_mkdir": True},
    )
    pipeline = dlt.pipeline(
        pipeline_name=_pipeline_name(settings.pipeline_name, spec.source_id),
        destination=destination,
        dataset_name="data",
    )
    return _SafePipelineProxy(
        pipeline,
        protect_partial_replace=spec.connector in _INCREMENTAL_PARTIAL_CONNECTORS,
    )
