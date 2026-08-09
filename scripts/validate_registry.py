#!/usr/bin/env python3
import csv,sys,urllib.parse
p='registry/sources.csv'; rows=list(csv.DictReader(open(p,encoding='utf-8')))
required={'source_id','title','domain','provider','source_url','rights_tier','access_tier','priority'}
errors=[]; seen=set()
for i,r in enumerate(rows,2):
    miss=[k for k in required if not r.get(k)]
    if miss: errors.append(f'row {i}: missing {miss}')
    if r.get('source_id') in seen: errors.append(f'row {i}: duplicate source_id')
    seen.add(r.get('source_id'))
    u=urllib.parse.urlsplit(r.get('source_url',''))
    if u.scheme not in {'http','https'} or not u.netloc: errors.append(f'row {i}: invalid URL')
print(f'{len(rows)} source records checked')
if errors:
    print('\n'.join(errors)); sys.exit(1)
print('registry OK')
