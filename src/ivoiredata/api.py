from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .delivery import inventory, source_paths
from .discoveries import data_gouv_discoveries
from .engine import IvoireDataEngine
from .query import query_source_sql
from .ranking import rank_sources
from .search import search_documents

app = FastAPI(title="IvoireData Engine", version="0.8.1")


class SQLRequest(BaseModel):
    sql: str
    max_rows: int = 1000


class UpdateSettingsRequest(BaseModel):
    automatic_enabled: bool | None = None
    scheduler_interval_seconds: int | None = None


class SourceSettingsRequest(BaseModel):
    enabled: bool | None = None
    update_mode: Literal["AUTOMATIC", "MANUAL", "DISABLED"] | None = None
    refresh_hours: int | None = None


@app.get("/health")
def health():
    engine = IvoireDataEngine()
    return {"status": "ok", "engine": "IvoireData", "version": "0.8.1", "storage": "local", "country_code": "CIV", "data_dir": str(engine.settings.data_dir)}


@app.get("/sources")
def sources(public_only: bool = False, domain: str | None = None, include_disabled: bool = False):
    engine = IvoireDataEngine()
    items = engine.registry.all() if include_disabled else engine.registry.list()
    if public_only: items = [s for s in items if s.public]
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
            "source_id": spec.source_id, "domain": spec.domain, "country_code": audit.get("country_code", "CIV"),
            "enabled": spec.enabled, "due": engine.freshness.due(spec), "refresh_hours": spec.refresh_hours,
            "auto_sync": spec.auto_sync, "last_success": state.get("last_success"),
            "last_status": state.get("last_status", "never"), "delivery_status": audit.get("delivery_status"),
            "freshness_status": audit.get("freshness_status"), "transport_security": audit.get("transport_security"),
            "rows": audit.get("rows", 0), "warnings": audit.get("warnings", []),
        })
    return {"rows": rows}


@app.get("/coverage")
def coverage(): return IvoireDataEngine().coverage()

@app.get("/coverage-audit")
def coverage_audit(): return IvoireDataEngine().coverage_audit()

@app.get("/quality-audit")
def quality_audit(): return IvoireDataEngine().quality_audit()

@app.get("/discoveries")
def discoveries(limit: int = 100): return data_gouv_discoveries(IvoireDataEngine(), limit=min(max(limit, 1), 1000))

@app.get("/ci-gold")
def ci_gold(): return IvoireDataEngine().ci_gold()

@app.post("/ci-gold/report")
def ci_gold_report(): return IvoireDataEngine().write_ci_gold()

@app.get("/qualification")
def qualification(): return IvoireDataEngine().qualification.status()

@app.post("/qualification/start")
def qualification_start(): return IvoireDataEngine().start_qualification()

@app.get("/audit")
def audit(public_only: bool = True): return IvoireDataEngine().audit(public_only=public_only)

@app.get("/inventory")
def data_inventory():
    engine = IvoireDataEngine(); return inventory(engine.settings, engine.registry.list())


@app.get("/settings/updates")
def update_settings():
    engine = IvoireDataEngine(); return engine.runtime.status(engine.registry)


@app.put("/settings/updates")
def set_update_settings(req: UpdateSettingsRequest):
    engine = IvoireDataEngine()
    try:
        engine.runtime.set_updates(automatic_enabled=req.automatic_enabled, scheduler_interval_seconds=req.scheduler_interval_seconds)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    updated = IvoireDataEngine(); return updated.runtime.status(updated.registry)


@app.get("/sources/{source_id}/settings")
def source_settings(source_id: str):
    engine = IvoireDataEngine()
    try: return engine.runtime.source_status(engine.registry, source_id)
    except KeyError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/sources/{source_id}/settings")
def set_source_settings(source_id: str, req: SourceSettingsRequest):
    engine = IvoireDataEngine()
    try:
        engine.registry.get(source_id)
        changes: dict[str, object] = {}
        if req.update_mode == "DISABLED": changes["enabled"] = False
        elif req.update_mode == "AUTOMATIC": changes.update(enabled=True, auto_sync=True)
        elif req.update_mode == "MANUAL": changes.update(enabled=True, auto_sync=False)
        if req.enabled is not None: changes["enabled"] = req.enabled
        if req.refresh_hours is not None: changes["refresh_hours"] = req.refresh_hours
        engine.runtime.set_source(source_id, **changes)
    except KeyError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc
    updated = IvoireDataEngine(); return updated.runtime.source_status(updated.registry, source_id)


@app.get("/sources/{source_id}/path")
def source_path(source_id: str):
    engine = IvoireDataEngine()
    try: spec = engine.registry.get(source_id)
    except KeyError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {k: str(v) for k, v in source_paths(engine.settings, spec).items()}


@app.post("/sync/{source_id}")
def sync(source_id: str, force: bool = False):
    try: result = IvoireDataEngine().sync(source_id, force=force)
    except (KeyError, PermissionError) as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.status != "success": raise HTTPException(status_code=502, detail=result.details)
    return result.__dict__


@app.get("/search/documents")
def document_search(q: str, limit: int = 20):
    try: return {"rows": search_documents(q, min(limit, 100))}
    except Exception as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/query/source/{source_id}")
def sql(source_id: str, req: SQLRequest):
    try: return {"rows": query_source_sql(source_id, req.sql, max_rows=min(req.max_rows, 5000))}
    except Exception as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc
