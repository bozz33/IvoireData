from __future__ import annotations
from fastapi import FastAPI,HTTPException
from pydantic import BaseModel
from .engine import IvoireDataEngine
from .query import query_sql
from .ranking import rank_sources
from .search import search_documents
app=FastAPI(title="IvoireData Engine",version="0.4.0")
class SQLRequest(BaseModel):sql:str;max_rows:int=1000
@app.get("/health")
def health():return {"status":"ok","engine":"IvoireData","version":"0.4.0"}
@app.get("/sources")
def sources(public_only:bool=False,domain:str|None=None):
    e=IvoireDataEngine();items=e.registry.list(public_only=public_only)
    if domain:items=[s for s in items if s.domain in {domain,"multidomain"}]
    return [{**s.__dict__,"rank":score} for s,score in rank_sources(items,domain=domain)]
@app.post("/sync/{source_id}")
def sync(source_id:str,force:bool=False):
    result=IvoireDataEngine().sync(source_id,force=force)
    if result.status!="success":raise HTTPException(status_code=502,detail=result.details)
    return result.__dict__
@app.get("/search/documents")
def document_search(q:str,limit:int=20):
    try:return {"rows":search_documents(q,min(limit,100))}
    except Exception as exc:raise HTTPException(status_code=400,detail=str(exc)) from exc
@app.post("/query/sql")
def sql(req:SQLRequest):
    try:return {"rows":query_sql(req.sql,max_rows=min(req.max_rows,5000))}
    except Exception as exc:raise HTTPException(status_code=400,detail=str(exc)) from exc
