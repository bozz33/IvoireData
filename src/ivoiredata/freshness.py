from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .locks import file_lock
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
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self.data = self._load()

    def _load(self) -> dict:
        payload = load_json(self.path, {})
        return payload if isinstance(payload, dict) else {}

    def refresh(self) -> None:
        self.data = self._load()

    def due(self, spec: SourceSpec, now: datetime | None = None) -> bool:
        self.refresh()
        now = now or datetime.now(timezone.utc)
        last = parse_time(self.data.get(spec.source_id, {}).get("last_success"))
        return last is None or now >= last + timedelta(hours=spec.refresh_hours)

    def mark(self, source_id: str, *, success: bool, now: datetime | None = None, details: str = "") -> None:
        now = now or datetime.now(timezone.utc)
        with file_lock(self.lock_path, timeout=60):
            self.data = self._load()
            row = self.data.setdefault(source_id, {})
            if not isinstance(row, dict):
                row = {}
                self.data[source_id] = row
            row["last_attempt"] = now.isoformat().replace("+00:00", "Z")
            row["last_status"] = "success" if success else "error"
            row["details"] = details[-2000:]
            if success:
                row["last_success"] = row["last_attempt"]
            atomic_write_json(self.path, self.data)
