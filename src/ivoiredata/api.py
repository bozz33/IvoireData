from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .delivery import inventory, source_paths
from .engine import IvoireDataEngine
from .query import query_source_sql
from .ranking import rank_sources
from .search import search_documents

app = FastAPI(title="IvoireData Engine", version="0.7.1")


class SQLRequest(BaseModel):
    sql: str
    max_rows: int = 1000


@app.get("/health")
def health():
    engine = IvoireDataEngine()
    return {"status": "ok", "engine": "IvoireData", "version": "0.7.1", "storage": "local", "data_dir": str(engine.settings.data_dir)}


@app.get("/sources")
def sources(public_only: bool = False, domain: str | None = None):
    engine = IvoireDataEngine(); items = engine.registry.list(public_only=public_only)
    if domain: items = [s for s in items if s.domain in {domain, "multidomain"}]
    return [{**s.__dict__, "rank": score} for s, score in rank_sources(items, domain=domain)]


@app.get("/status")
def status(public_only: bool = True):
    engine = IvoireDataEngine(); rows = []
    audit_rows = {row["source_id"]: row for row in engine.audit(public_only=public_only)["rows"]}
    for spec in engine.registry.list(public_only=public_only):
        state = engine.freshness.data.get(spec.source_id, {})
        audit = audit_rows.get(spec.source_id, {})
        rows.append({
            "source_id": spec.source_id,
            "domain": spec.domain,
            "due": engine.freshness.due(spec),
            "refresh_hours": spec.refresh_hours,
            "auto_sync": spec.auto_sync,
            "last_success": state.get("last_success"),
            "last_status": state.get("last_status", "never"),
            "delivery_status": audit.get("delivery_status"),
            "freshness_status": audit.get("freshness_status"),
            "transport_security": audit.get("transport_security"),
            "rows": audit.get("rows", 0),
            "warnings": audit.get("warnings", []),
        })
    return {"rows": rows}


@app.get("/coverage")
def coverage():
    return IvoireDataEngine().coverage()


@app.get("/audit")
def audit(public_only: bool = True):
    return IvoireDataEngine().audit(public_only=public_only)


@app.get("/inventory")
def data_inventory():
    engine = IvoireDataEngine()
    return inventory(engine.settings, engine.registry.list())


@app.get("/sources/{source_id}/path")
def source_path(source_id: str):
    engine = IvoireDataEngine()
    try:
        spec = engine.registry.get(source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {k: str(v) for k, v in source_paths(engine.settings, spec).items()}


@app.post("/sync/{source_id}")
def sync(source_id: str, force: bool = False):
    try:
        result = IvoireDataEngine().sync(source_id, force=force)
    except (KeyError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.status != "success":
        raise HTTPException(status_code=502, detail=result.details)
    return result.__dict__


@app.get("/search/documents")
def document_search(q: str, limit: int = 20):
    try:
        return {"rows": search_documents(q, min(limit, 100))}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/query/source/{source_id}")
def sql(source_id: str, req: SQLRequest):
    try:
        return {"rows": query_source_sql(source_id, req.sql, max_rows=min(req.max_rows, 5000))}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
