from __future__ import annotations

import re

from .delivery import ensure_source_layout
from .models import SourceSpec
from .settings import Settings

_SAFE = re.compile(r"[^a-zA-Z0-9_]+")


def _pipeline_name(base: str, source_id: str) -> str:
    suffix = _SAFE.sub("_", source_id).strip("_")
    return f"{base}_{suffix}"[:120]


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
    """
    import dlt
    from dlt.destinations import filesystem

    paths = ensure_source_layout(settings, spec)
    destination = filesystem(
        bucket_url=paths["tables"].resolve().as_uri(),
        layout="{table_name}/{load_package_timestamp}/{load_id}.{file_id}.{ext}",
        kwargs={"auto_mkdir": True},
    )
    return dlt.pipeline(
        pipeline_name=_pipeline_name(settings.pipeline_name, spec.source_id),
        destination=destination,
        dataset_name="data",
    )
