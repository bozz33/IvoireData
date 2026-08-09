#!/usr/bin/env python3
"""Run the generated public ingestion queue conservatively."""
import argparse,json,subprocess,sys
ap=argparse.ArgumentParser(); ap.add_argument('--max-items',type=int,default=10); ap.add_argument('--source-id'); a=ap.parse_args()
rows=[json.loads(x) for x in open('data/manifests/public_ingestion_queue.jsonl',encoding='utf-8') if x.strip()]
if a.source_id: rows=[r for r in rows if r['source_id']==a.source_id]
rows=rows[:a.max_items]
failed=0
for r in rows:
    print('INGEST',r['source_id'],r['url'],flush=True)
    rc=subprocess.call([sys.executable,'scripts/ingest_public_web.py','--source-id',r['source_id'],r['url']])
    failed += (rc!=0)
print(f'done={len(rows)} failed={failed}')
raise SystemExit(1 if failed else 0)
