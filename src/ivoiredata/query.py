from __future__ import annotations
import re
from typing import Any
from .pipeline import get_pipeline
from .settings import Settings
_SAFE_SQL=re.compile(r"^\s*(select|with)\b",re.I)
def query_sql(sql:str,settings:Settings|None=None,max_rows:int=1000)->list[dict[str,Any]]:
    if not _SAFE_SQL.search(sql) or ";" in sql.strip().rstrip(";"):raise ValueError("only one read-only SELECT/WITH query is allowed")
    settings=settings or Settings.from_env();pipeline=get_pipeline(settings)
    with pipeline.sql_client() as client:
        with client.execute_query(sql) as cursor:names=[col[0] for col in (cursor.description or [])];rows=cursor.fetchmany(max_rows)
    return [dict(zip(names,row)) for row in rows]
