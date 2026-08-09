#!/usr/bin/env python3
"""Bounded same-host crawler to discover public documents linked from an official site."""
from __future__ import annotations
import argparse, collections, html, json, re, time
import urllib.parse, urllib.request, urllib.robotparser
from datetime import datetime, timezone
UA='IvoireData/0.2 (+https://github.com/bozz33/IvoireData)'
DOC_EXT=('.pdf','.csv','.xls','.xlsx','.json','.xml','.doc','.docx','.zip','.geojson','.shp')

def robot_ok(url):
    u=urllib.parse.urlsplit(url); rp=urllib.robotparser.RobotFileParser(); rp.set_url(f'{u.scheme}://{u.netloc}/robots.txt')
    try: rp.read(); return rp.can_fetch(UA,url)
    except Exception: return True

def get(url):
    req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'text/html,*/*;q=0.5'})
    with urllib.request.urlopen(req,timeout=60) as r:
        return r.read(5_000_000),r.headers.get_content_type(),r.geturl()

def links(body,base):
    text=body.decode('utf-8','replace')
    for href in re.findall(r'(?is)href\s*=\s*["\']([^"\']+)["\']',text):
        u=urllib.parse.urljoin(base,html.unescape(href)).split('#')[0]
        if u.startswith(('http://','https://')): yield u

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('url'); ap.add_argument('--source-id',required=True)
    ap.add_argument('--max-pages',type=int,default=50); ap.add_argument('--delay',type=float,default=1.5); ap.add_argument('--include-pages',action='store_true')
    a=ap.parse_args(); root=urllib.parse.urlsplit(a.url); host=root.netloc.lower()
    q=collections.deque([a.url]); seen=set(); docs=set(); pages=[]
    while q and len(seen)<a.max_pages:
        u=q.popleft()
        if u in seen or urllib.parse.urlsplit(u).netloc.lower()!=host or not robot_ok(u): continue
        seen.add(u); time.sleep(max(0,a.delay))
        try: body,ctype,final=get(u)
        except Exception as e:
            pages.append({'url':u,'status':'error','error':str(e)}); continue
        pages.append({'url':final,'status':'ok','content_type':ctype})
        if any(urllib.parse.urlsplit(final).path.lower().endswith(x) for x in DOC_EXT): docs.add(final); continue
        if ctype!='text/html': continue
        for x in links(body,final):
            if urllib.parse.urlsplit(x).netloc.lower()!=host: continue
            path=urllib.parse.urlsplit(x).path.lower()
            if path.endswith(DOC_EXT): docs.add(x)
            elif a.include_pages and x not in seen: q.append(x)
    now=datetime.now(timezone.utc).isoformat()
    out={'source_id':a.source_id,'seed_url':a.url,'retrieved_at':now,'pages_scanned':len(seen),'documents':sorted(docs),'pages':pages}
    import pathlib
    p=pathlib.Path('data/manifests/discovery'); p.mkdir(parents=True,exist_ok=True)
    target=p/(a.source_id+'.json'); target.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'source_id':a.source_id,'pages_scanned':len(seen),'documents_found':len(docs),'manifest':str(target)},ensure_ascii=False))
if __name__=='__main__': main()
