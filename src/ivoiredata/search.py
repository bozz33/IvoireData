from __future__ import annotations
from .query import query_sql
def search_documents(query:str,max_rows:int=20):
    terms=[t for t in query.strip().split() if len(t)>2][:8]
    if not terms:return []
    clauses=[]
    for term in terms:
        safe=term.replace("'","''").replace("%","\\%").replace("_","\\_");clauses.append(f"text ILIKE '%{safe}%' ESCAPE '\\\\'")
    return query_sql("SELECT source_id, source_url, chunk_id, text FROM public_documents WHERE "+" OR ".join(clauses)+f" LIMIT {int(max_rows)}",max_rows=max_rows)
