#!/usr/bin/env python3
import csv,json,pathlib
rows=list(csv.DictReader(open('registry/sources.csv',encoding='utf-8')))
out=[]
for r in rows:
    rights=r['rights_tier']
    if rights.startswith('D_'): continue
    if r['domain'].startswith('language_'): continue
    raw_action='FETCH_OPEN' if rights=='A_REDISTRIBUTABLE' else 'LOCAL_CACHE_ONLY'
    out.append({'source_id':r['source_id'],'url':r['source_url'],'domain':r['domain'],'rights_tier':rights,'raw_action':raw_action,'derived_action':'BUILD_FACTS_AND_RAG'})
p=pathlib.Path('data/manifests/public_ingestion_queue.jsonl'); p.parent.mkdir(parents=True,exist_ok=True)
p.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in out),encoding='utf-8')
print(f'{len(out)} sources queued -> {p}')
