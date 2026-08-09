from __future__ import annotations
import math,re
_WORD=re.compile(r"\w+",re.UNICODE)
def score_text(text:str)->float:
    if not text:return 0.0
    n=len(text); words=_WORD.findall(text)
    if n<80 or len(words)<12:return 0.15
    alpha=sum(ch.isalpha() for ch in text)/max(n,1); unique=len(set(w.casefold() for w in words))/max(len(words),1); length_score=min(1.0,math.log10(max(n,10))/4.0)
    return round(max(0.0,min(1.0,0.45*alpha+0.35*min(1.0,unique*2.0)+0.20*length_score)),4)
def acceptable(text:str,threshold:float=0.35)->bool:return score_text(text)>=threshold
