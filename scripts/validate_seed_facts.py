#!/usr/bin/env python3
import json,urllib.parse
n=0
for line in open('data/seed/public_facts.jsonl',encoding='utf-8'):
    if not line.strip(): continue
    x=json.loads(line); n+=1
    for k in ('id','domain','metric','value','date','source_url','provider'):
        assert k in x and x[k] not in ('',None),(n,k)
    u=urllib.parse.urlsplit(x['source_url']); assert u.scheme=='https' and u.netloc
print(f'OK: {n} seed facts')
