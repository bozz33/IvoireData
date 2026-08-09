from __future__ import annotations

from .query import query_source_sql
from .registry import SourceRegistry
from .settings import Settings


def search_documents(query: str, max_rows: int = 20, settings: Settings | None = None):
    terms = [t for t in query.strip().split() if len(t) > 2][:8]
    if not terms:
        return []
    settings = settings or Settings.from_env()
    registry = SourceRegistry.load(settings.registry_path, settings.runtime_config_path)
    clauses = []
    for term in terms:
        safe = term.replace("'", "''").replace("%", "\\%").replace("_", "\\_")
        clauses.append(f"text ILIKE '%{safe}%' ESCAPE '\\\\'")
    sql = "SELECT source_id, source_url, chunk_id, text FROM public_documents WHERE " + " OR ".join(clauses) + f" LIMIT {int(max_rows)}"
    rows = []
    for spec in registry.list(public_only=True):
        if spec.connector != "public_web":
            continue
        try:
            for row in query_source_sql(spec.source_id, sql, settings=settings, max_rows=max_rows - len(rows)):
                row["domain"] = spec.domain
                rows.append(row)
                if len(rows) >= max_rows:
                    return rows
        except Exception:
            continue
    return rows
