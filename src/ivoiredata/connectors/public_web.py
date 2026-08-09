from __future__ import annotations
import hashlib,io
from html.parser import HTMLParser
from ..cleaning import clean_text
class _HTMLText(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag.lower() in {"script","style","noscript"}:self.skip+=1
    def handle_endtag(self,tag):
        if tag.lower() in {"script","style","noscript"} and self.skip:self.skip-=1
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)
def html_text(raw:bytes)->str:
    p=_HTMLText();p.feed(raw.decode("utf-8","replace"));return clean_text("\n".join(p.parts))
def chunk_text(text:str,size:int=3500,overlap:int=250):
    text=clean_text(text)
    if not text:return
    start=0
    while start<len(text):
        end=min(len(text),start+size);yield text[start:end]
        if end==len(text):break
        start=max(start+1,end-overlap)
def public_document_resource(*,source_id:str,url:str,user_agent:str="IvoireData/0.4",force:bool=False):
    import dlt,requests
    from pypdf import PdfReader
    @dlt.resource(name="public_documents",write_disposition="merge",primary_key="chunk_id")
    def resource():
        state=dlt.current.resource_state().setdefault("content_hashes",{});r=requests.get(url,timeout=120,headers={"User-Agent":user_agent});r.raise_for_status();digest=hashlib.sha256(r.content).hexdigest()
        if not force and state.get(url)==digest:return
        ctype=r.headers.get("content-type","").lower()
        if "pdf" in ctype or url.lower().split("?",1)[0].endswith(".pdf"):
            reader=PdfReader(io.BytesIO(r.content));text="\n".join((page.extract_text() or "") for page in reader.pages)
        elif "html" in ctype or r.content.lstrip().startswith(b"<"):text=html_text(r.content)
        else:text=clean_text(r.content.decode("utf-8","replace"))
        for idx,chunk in enumerate(chunk_text(text)):
            chunk_id=hashlib.sha256(f"{source_id}|{url}|{digest}|{idx}".encode()).hexdigest();yield {"chunk_id":chunk_id,"source_id":source_id,"source_url":url,"content_sha256":digest,"chunk_index":idx,"text":chunk}
        state[url]=digest
    return resource()
