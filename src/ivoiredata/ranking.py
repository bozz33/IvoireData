from __future__ import annotations
from .models import SourceSpec
_PRIORITY={"P0":1.0,"P1":0.85,"P2":0.7,"P3":0.55}
def source_score(spec:SourceSpec,*,domain:str|None=None)->float:
    score=_PRIORITY.get(spec.priority.upper(),0.5);provider=spec.provider.casefold()
    if any(x in provider for x in ("minist","direction","anstat","bceao","gouv","office","autorité","autorite")):score+=0.08
    if spec.rights_tier.startswith("A_"):score+=0.03
    if domain and (spec.domain==domain or spec.domain=="multidomain"):score+=0.08
    return round(min(1.0,score),4)
def rank_sources(sources:list[SourceSpec],*,domain:str|None=None):return sorted(((s,source_score(s,domain=domain)) for s in sources),key=lambda x:x[1],reverse=True)
