from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import SyncResult
from .state_io import atomic_write_json, load_json


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

    Automatic scheduler cycles are the only cycles that count toward the
    14-day stability window. A clean preflight/full-sync may be snapshotted as
    a baseline so long refresh intervals do not make a 14-day qualification
    mathematically impossible.
    """

    def __init__(self, path: Path):
        self.path = path
        payload = load_json(path, {})
        self.data = payload if isinstance(payload, dict) else {}

    def _save(self) -> None:
        atomic_write_json(self.path, self.data)

    def start(self, baseline_sources: Iterable[str] | None = None) -> dict:
        now = _now()
        baseline = sorted({str(source_id) for source_id in (baseline_sources or []) if source_id})
        self.data = {
            "started_at": now,
            "baseline_at": now,
            "baseline_sources": baseline,
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

    def reset(self, baseline_sources: Iterable[str] | None = None) -> dict:
        return self.start(baseline_sources=baseline_sources)

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
        baseline_sources = sorted({str(x) for x in (self.data.get("baseline_sources") or []) if x})
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
            "baseline_at": self.data.get("baseline_at"),
            "baseline_sources": baseline_sources,
            "baseline_source_count": len(baseline_sources),
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
