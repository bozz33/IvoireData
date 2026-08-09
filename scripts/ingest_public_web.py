#!/usr/bin/env python3
"""Fetch one public URL into a provenance-rich local RAG snapshot.
Raw bytes stay in gitignored local storage when redistribution rights are unclear.
No authentication, CAPTCHA, paywall or role bypass.
"""
from __future__ import annotations
import argparse, hashlib, html, io, json, pathlib, re, time
import urllib.parse, urllib.request, urllib.robotparser
from datetime import datetime, timezone

UA='IvoireData/0.2 (+https://github.com/bozz33/IvoireData)'

def allowed(url:str)->bool:
    u=urllib.parse.urlsplit(url)
    rp=urllib.robotparser.RobotFileParser(); rp.set_url(f'{u.scheme}://{u.netloc}/robots.txt')
    try:
        rp.read(); return rp.can_fetch(UA,url)
    except Exception:
        return True

def fetch(url:str,max_mb:int=100):
    req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'text/html,application/pdf,text/plain,*/*;q=0.5'})
    with urllib.request.urlopen(req,timeout=90) as r:
        data=r.read(max_mb*1024*1024+1)
        if len(data)>max_mb*1024*1024: raise RuntimeError('file exceeds max size')
        return data,r.headers.get_content_type(),r.geturl()

def html_text(data:bytes)->str:
    s=data.decode('utf-8','replace')
    s=re.sub(r'(?is)<(script|style|noscript).*?>.*?</\1>',' ',s)
    s=re.sub(r'(?s)<[^>]+>',' ',s)
    return re.sub(r'\s+',' ',html.unescape(s)).strip()

def pdf_text(data:bytes)->str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError('pip install pypdf') from e
    return '\n'.join((p.extract_text() or '') for p in PdfReader(io.BytesIO(data)))

def chunks(text:str,size:int=2200,overlap:int=250):
    text=re.sub(r'\s+',' ',text).strip(); i=0
    while i<len(text):
        j=min(len(text),i+size); yield text[i:j]
        if j==len(text): break
        i=max(i+1,j-overlap)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('url'); ap.add_argument('--source-id',required=True)
    ap.add_argument('--max-mb',type=int,default=100); ap.add_argument('--delay',type=float,default=1.5)
    a=ap.parse_args()
    if not allowed(a.url): raise SystemExit('robots.txt disallows this URL')
    time.sleep(max(0,a.delay)); data,ctype,final=fetch(a.url,a.max_mb)
    sha=hashlib.sha256(data).hexdigest(); now=datetime.now(timezone.utc).isoformat()
    raw=pathlib.Path('data/raw/public')/a.source_id; raw.mkdir(parents=True,exist_ok=True)
    ext='.pdf' if ctype=='application/pdf' else '.html' if 'html' in ctype else '.bin'
    (raw/(sha+ext)).write_bytes(data)
    text=pdf_text(data) if ctype=='application/pdf' else html_text(data) if 'html' in ctype else data.decode('utf-8','replace')
    out=pathlib.Path('data/processed/rag')/a.source_id; out.mkdir(parents=True,exist_ok=True)
    records=[]
    for idx,ch in enumerate(chunks(text)):
        records.append({'id':f'{a.source_id}:{sha[:12]}:{idx:05d}','source_id':a.source_id,'source_url':a.url,'final_url':final,'retrieved_at':now,'sha256_raw':sha,'content_type':ctype,'chunk_index':idx,'text':ch})
    (out/(sha+'.jsonl')).write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in records),encoding='utf-8')
    print(json.dumps({'source_id':a.source_id,'url':final,'sha256':sha,'chunks':len(records)},ensure_ascii=False))

if __name__=='__main__': main()
