from __future__ import annotations
from collections import defaultdict
from typing import Any
from .provenance import Claim
def reconcile_claims(claims:list[Claim])->dict[str,Any]:
    if not claims:return {"status":"no_data","confidence":0.0,"claims":[]}
    groups=defaultdict(list)
    for claim in claims:groups[repr(claim.value)].append(claim)
    ranked=sorted(groups.values(),key=lambda g:(sum(c.authority for c in g),len(g)),reverse=True);winner=ranked[0];total=sum(max(c.authority,0.01) for c in claims);support=sum(max(c.authority,0.01) for c in winner);confidence=min(1.0,support/total+(0.1 if len(winner)>1 else 0.0))
    return {"status":"consensus" if len(ranked)==1 else "conflict","value":winner[0].value,"confidence":round(confidence,4),"supporting_sources":[c.source_id for c in winner],"alternatives":[{"value":g[0].value,"sources":[c.source_id for c in g],"authority":round(sum(c.authority for c in g),4)} for g in ranked[1:]]}
