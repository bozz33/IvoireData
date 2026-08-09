from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .models import SourceSpec

def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None

class FreshnessStore:
    def __init__(self, path: Path):
        self.path=path; self.data={}
        if path.exists(): self.data=json.loads(path.read_text(encoding="utf-8"))
    def due(self, spec: SourceSpec, now: datetime | None=None) -> bool:
        now=now or datetime.now(timezone.utc); last=parse_time(self.data.get(spec.source_id,{}).get("last_success"))
        return last is None or now >= last + timedelta(hours=spec.refresh_hours)
    def mark(self, source_id: str, *, success: bool, now: datetime | None=None, details: str="") -> None:
        now=now or datetime.now(timezone.utc); row=self.data.setdefault(source_id,{})
        row["last_attempt"]=now.isoformat().replace("+00:00","Z"); row["last_status"]="success" if success else "error"; row["details"]=details[-2000:]
        if success: row["last_success"]=row["last_attempt"]
        self.path.parent.mkdir(parents=True,exist_ok=True); self.path.write_text(json.dumps(self.data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
