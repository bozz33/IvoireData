from __future__ import annotations
import hashlib
from .cleaning import clean_text
def text_fingerprint(text: str) -> str: return hashlib.sha256(clean_text(text).casefold().encode("utf-8")).hexdigest()
class ExactDeduplicator:
    def __init__(self): self._seen=set()
    def accept(self,text:str):
        fp=text_fingerprint(text)
        if fp in self._seen: return False,fp
        self._seen.add(fp); return True,fp
