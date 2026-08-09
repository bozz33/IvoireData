from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timezone
from typing import Any
@dataclass(frozen=True)
class Claim:
    value:Any;source_id:str;source_url:str;authority:float=0.5;observed_at:str="";period:str|None=None;unit:str|None=None
    def __post_init__(self):
        if not self.observed_at:object.__setattr__(self,"observed_at",datetime.now(timezone.utc).isoformat().replace("+00:00","Z"))
