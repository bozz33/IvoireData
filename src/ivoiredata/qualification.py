from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import SyncResult


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class QualificationStore:
    """Persistent CI Gold stability qualification ledger.

    Only automatic scheduler cycles are recorded. Manual syncs are deliberately
    excluded so repair/test runs cannot falsify the stability window.
    """

    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def start(self) -> dict:
        now = _now()
        self.data = {
            "started_at": now,
            "last_cycle_at": None,
            "cycles_total": 0,
            "cycles_with_errors": 0,
            "sync_attempts": 0,
            "sync_successes": 0,
            "sync_errors": 0,
            "source_attempts": {},
            "source_errors": {},
            "last_errors": [],
        }
        self._save()
        return self.status()

    def reset(self) -> dict:
        return self.start()

    def record_cycle(self, results: Iterable[SyncResult]) -> dict:
        if not self.data.get("started_at"):
            self.start()
        rows = list(results)
        errors = [row.source_id for row in rows if row.status != "success"]
        source_attempts = self.data.setdefault("source_attempts", {})
        source_errors = self.data.setdefault("source_errors", {})
        for row in rows:
            source_attempts[row.source_id] = int(source_attempts.get(row.source_id, 0)) + 1
            if row.status != "success":
                source_errors[row.source_id] = int(source_errors.get(row.source_id, 0)) + 1
        self.data["last_cycle_at"] = _now()
        self.data["cycles_total"] = int(self.data.get("cycles_total", 0)) + 1
        self.data["sync_attempts"] = int(self.data.get("sync_attempts", 0)) + len(rows)
        self.data["sync_successes"] = int(self.data.get("sync_successes", 0)) + sum(1 for row in rows if row.status == "success")
        self.data["sync_errors"] = int(self.data.get("sync_errors", 0)) + len(errors)
        if errors:
            self.data["cycles_with_errors"] = int(self.data.get("cycles_with_errors", 0)) + 1
        self.data["last_errors"] = errors
        self._save()
        return self.status()

    def status(self) -> dict:
        started = _parse(self.data.get("started_at"))
        now = datetime.now(timezone.utc)
        elapsed_days = 0.0
        if started:
            elapsed_days = max(0.0, (now - started).total_seconds() / 86400.0)
        cycles = int(self.data.get("cycles_total", 0))
        error_cycles = int(self.data.get("cycles_with_errors", 0))
        attempts = int(self.data.get("sync_attempts", 0))
        successes = int(self.data.get("sync_successes", 0))
        errors = int(self.data.get("sync_errors", 0))
        source_attempts = {str(k): int(v) for k, v in (self.data.get("source_attempts") or {}).items()}
        source_errors = {str(k): int(v) for k, v in (self.data.get("source_errors") or {}).items()}
        qualified = bool(
            started
            and elapsed_days >= 14
            and cycles >= 14
            and attempts > 0
            and successes > 0
            and error_cycles == 0
            and errors == 0
        )
        return {
            "started_at": self.data.get("started_at"),
            "last_cycle_at": self.data.get("last_cycle_at"),
            "elapsed_days": round(elapsed_days, 3),
            "cycles_total": cycles,
            "cycles_with_errors": error_cycles,
            "successful_cycles": max(0, cycles - error_cycles),
            "sync_attempts": attempts,
            "sync_successes": successes,
            "sync_errors": errors,
            "sources_attempted": sorted(source_attempts),
            "source_attempts": source_attempts,
            "sources_with_errors": sorted(source_errors),
            "source_errors": source_errors,
            "last_errors": list(self.data.get("last_errors") or []),
            "qualification_days_required": 14,
            "minimum_cycles_required": 14,
            "requires_real_sync_attempt": True,
            "qualified": qualified,
        }
