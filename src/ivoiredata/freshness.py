from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import SourceSpec
from .state_io import atomic_write_json, load_json


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


class FreshnessStore:
    def __init__(self, path: Path):
        self.path = path
        payload = load_json(path, {})
        self.data = payload if isinstance(payload, dict) else {}

    def due(self, spec: SourceSpec, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        last = parse_time(self.data.get(spec.source_id, {}).get("last_success"))
        return last is None or now >= last + timedelta(hours=spec.refresh_hours)

    def mark(self, source_id: str, *, success: bool, now: datetime | None = None, details: str = "") -> None:
        now = now or datetime.now(timezone.utc)
        row = self.data.setdefault(source_id, {})
        row["last_attempt"] = now.isoformat().replace("+00:00", "Z")
        row["last_status"] = "success" if success else "error"
        row["details"] = details[-2000:]
        if success:
            row["last_success"] = row["last_attempt"]
        atomic_write_json(self.path, self.data)
