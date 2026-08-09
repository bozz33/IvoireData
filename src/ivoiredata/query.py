from __future__ import annotations

import re
from typing import Any

from .pipeline import get_pipeline, get_source_pipeline
from .registry import SourceRegistry
from .settings import Settings

_SAFE_SQL = re.compile(r"^\s*(select|with)\b", re.I)


def _validate(sql: str) -> None:
    if not _SAFE_SQL.search(sql) or ";" in sql.strip().rstrip(";"):
        raise ValueError("only one read-only SELECT/WITH query is allowed")


def _execute(pipeline, sql: str, max_rows: int) -> list[dict[str, Any]]:
    with pipeline.sql_client() as client:
        with client.execute_query(sql) as cursor:
            names = [col[0] for col in (cursor.description or [])]
            rows = cursor.fetchmany(max_rows)
    return [dict(zip(names, row)) for row in rows]


def query_source_sql(source_id: str, sql: str, settings: Settings | None = None, max_rows: int = 1000) -> list[dict[str, Any]]:
    _validate(sql)
    settings = settings or Settings.from_env()
    registry = SourceRegistry.load(settings.registry_path, settings.runtime_config_path)
    spec = registry.get(source_id)
    return _execute(get_source_pipeline(settings, spec), sql, max_rows)


def query_sql(sql: str, settings: Settings | None = None, max_rows: int = 1000) -> list[dict[str, Any]]:
    """Backward-compatible query for the pre-v0.6 global dataset."""
    _validate(sql)
    settings = settings or Settings.from_env()
    return _execute(get_pipeline(settings), sql, max_rows)
