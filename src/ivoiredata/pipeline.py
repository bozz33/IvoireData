from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable

from .delivery import ensure_source_layout
from .dlt_tables import make_incremental_replace_safe
from .locks import file_lock
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


def _source_lock_timeout(spec: SourceSpec | None = None) -> float:
    """Resolve the lock wait independently from HTTP/network timeouts.

    Historically every source could wait 21,600 seconds (6h) for its dlt/file lock.
    That is acceptable for some large manual CI jobs, but disastrous for the bounded
    technology worker because an idle lock wait looks like a network hang and keeps an
    orchestrator cycle RUNNING for hours.  Dynamic callers can now set a much shorter
    per-source timeout while existing sources retain the legacy environment/default.
    """

    configured = None
    if spec is not None:
        configured = spec.options.get("source_lock_timeout_seconds")
    if configured in (None, ""):
        configured = os.getenv("IVOIREDATA_SOURCE_LOCK_TIMEOUT") or 21600
    try:
        return max(1.0, float(configured))
    except (TypeError, ValueError):
        return 21600.0


class _SafePipelineProxy:
    """Delegate to dlt while isolating tables and serializing one source at a time."""

    def __init__(
        self,
        pipeline: Any,
        *,
        protect_partial_replace: bool,
        lock_path: Path | None = None,
        lock_timeout_seconds: float | None = None,
        post_success: Callable[[], Any] | None = None,
    ):
        self._pipeline = pipeline
        self._protect_partial_replace = protect_partial_replace
        self._lock_path = lock_path
        self._lock_timeout_seconds = lock_timeout_seconds
        self._post_success = post_success

    def __getattr__(self, name: str):
        return getattr(self._pipeline, name)

    def _run(self, data: Any, *args: Any, **kwargs: Any):
        if self._protect_partial_replace and hasattr(data, "apply_hints") and hasattr(data, "add_map"):
            data = make_incremental_replace_safe(data)
        result = self._pipeline.run(data, *args, **kwargs)
        if self._post_success is not None:
            self._post_success()
        return result

    def run(self, data: Any, *args: Any, **kwargs: Any):
        if self._lock_path is None:
            return self._run(data, *args, **kwargs)
        timeout = self._lock_timeout_seconds
        if timeout is None:
            timeout = _source_lock_timeout(None)
        # API, scheduler and sync-once share `.ivoiredata`; only the same source is
        # serialized. Different sources remain free to run independently.
        with file_lock(self._lock_path, timeout=max(1.0, float(timeout))):
            return self._run(data, *args, **kwargs)


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
    A shared file lock also prevents concurrent runs of the *same* source from API,
    scheduler and one-shot containers. Source-specific migration cleanup runs only
    after dlt has committed successfully and while that same lock is still held.
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

    def post_success():
        from .post_sync import cleanup_after_success

        cleanup_after_success(settings, spec)

    return _SafePipelineProxy(
        pipeline,
        protect_partial_replace=spec.connector in _INCREMENTAL_PARTIAL_CONNECTORS,
        lock_path=settings.state_dir / "locks" / f"{_SAFE.sub('_', spec.source_id)}.lock",
        lock_timeout_seconds=_source_lock_timeout(spec),
        post_success=post_success,
    )
