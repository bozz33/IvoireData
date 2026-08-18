from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .deadline import HardDeadlineExceeded, hard_deadline, hard_deadline_supported
from .delivery import source_paths
from .models import SyncResult
from .state_io import atomic_write_json
from .technology_documentation_fetch import DynamicDocumentationFetcher as _BaseFetcher


DEFAULT_DYNAMIC_LOCK_TIMEOUT_SECONDS = 120.0
DEFAULT_FETCH_TARGET_TIMEOUT_SECONDS = 900.0


def _env_seconds(name: str, default: float, *, minimum: float = 1.0) -> float:
    raw = os.getenv(name)
    try:
        value = float(raw) if raw is not None and raw.strip() else float(default)
    except (TypeError, ValueError):
        value = float(default)
    return max(float(minimum), value)


def _iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _watchdog_path(fetcher: _BaseFetcher) -> Path:
    return fetcher.settings.state_dir / "technology_fetch_active.json"


def _write_watchdog(fetcher: _BaseFetcher, payload: dict[str, Any]) -> None:
    atomic_write_json(_watchdog_path(fetcher), payload)


def _write_timeout_stats(
    fetcher: _BaseFetcher,
    spec,
    *,
    status: str,
    details: str,
    hard_timeout_seconds: float,
    lock_timeout_seconds: float,
) -> None:
    try:
        path = source_paths(fetcher.settings, spec)["raw"] / "official_docs_sync_stats.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            path,
            {
                "source_id": spec.source_id,
                "root_url": spec.source_url,
                "final_root_url": spec.source_url,
                "source_strategy": str(spec.options.get("source_strategy") or "AUTO"),
                "discovery_complete": False,
                "discovery_truncated": False,
                "failed": 1,
                "backlog_count": 1,
                "hard_timeout": status == "HARD_TIMEOUT",
                "source_lock_timeout": status == "LOCK_TIMEOUT",
                "hard_timeout_seconds": hard_timeout_seconds,
                "source_lock_timeout_seconds": lock_timeout_seconds,
                "last_error": details[:2000],
                "watchdog_recorded_at": _iso(),
            },
        )
    except Exception:
        # Timeout observability must never turn a retryable fetch failure into a second
        # failure that hides the original reason.
        pass


def _mark_sync_failure(fetcher: _BaseFetcher, spec, details: str) -> None:
    """Best-effort freshness/manifest failure marker after a hard deadline."""

    try:
        engine = fetcher._engine()
        finished = _iso()
        engine.freshness.mark(spec.source_id, success=False, details=details)
        engine._write_manifest(
            spec,
            status="error",
            started=finished,
            finished=finished,
            details=details,
        )
    except Exception:
        pass


_original_spec = _BaseFetcher._spec
_original_run_syncer = _BaseFetcher._run_syncer


def _bounded_spec(self: _BaseFetcher, target: dict[str, Any]):
    spec = _original_spec(self, target)
    options = dict(spec.options)
    options["source_lock_timeout_seconds"] = _env_seconds(
        "IVOIREDATA_TECH_FETCH_LOCK_TIMEOUT",
        DEFAULT_DYNAMIC_LOCK_TIMEOUT_SECONDS,
    )
    options["fetch_target_timeout_seconds"] = _env_seconds(
        "IVOIREDATA_TECH_FETCH_TARGET_TIMEOUT",
        DEFAULT_FETCH_TARGET_TIMEOUT_SECONDS,
    )
    return replace(spec, options=options)


def _watched_run_syncer(self: _BaseFetcher, spec, force: bool) -> SyncResult:
    hard_timeout_seconds = _env_seconds(
        "IVOIREDATA_TECH_FETCH_TARGET_TIMEOUT",
        spec.options.get("fetch_target_timeout_seconds", DEFAULT_FETCH_TARGET_TIMEOUT_SECONDS),
    )
    lock_timeout_seconds = _env_seconds(
        "IVOIREDATA_TECH_FETCH_LOCK_TIMEOUT",
        spec.options.get("source_lock_timeout_seconds", DEFAULT_DYNAMIC_LOCK_TIMEOUT_SECONDS),
    )
    started = _iso()
    base_state = {
        "status": "RUNNING",
        "source_id": spec.source_id,
        "package_registry": spec.options.get("package_registry"),
        "package_name": spec.options.get("package_name"),
        "target_url": spec.source_url,
        "started_at": started,
        "pid": os.getpid(),
        "watchdog_supported": hard_deadline_supported(),
        "hard_timeout_seconds": hard_timeout_seconds,
        "source_lock_timeout_seconds": lock_timeout_seconds,
    }
    _write_watchdog(self, base_state)

    try:
        with hard_deadline(
            hard_timeout_seconds,
            label=f"dynamic documentation fetch {spec.source_id}",
        ):
            result = _original_run_syncer(self, spec, force)
    except HardDeadlineExceeded as exc:
        finished = _iso()
        details = str(exc)
        _write_timeout_stats(
            self,
            spec,
            status="HARD_TIMEOUT",
            details=details,
            hard_timeout_seconds=hard_timeout_seconds,
            lock_timeout_seconds=lock_timeout_seconds,
        )
        _mark_sync_failure(self, spec, details)
        _write_watchdog(
            self,
            {
                **base_state,
                "status": "HARD_TIMEOUT",
                "finished_at": finished,
                "last_error": details,
            },
        )
        # Returning an ordinary failed SyncResult lets the existing fetcher persist
        # RETRY/backoff normally.  v2 migration therefore remains PENDING and the old
        # corpus stays live, exactly like any other transient failure.
        return SyncResult(
            spec.source_id,
            "error",
            started,
            finished,
            spec.connector,
            details,
        )
    except BaseException:
        _write_watchdog(
            self,
            {
                **base_state,
                "status": "ABORTED",
                "finished_at": _iso(),
            },
        )
        raise

    details = str(getattr(result, "details", "") or "")
    lock_timeout = "timed out waiting for lock:" in details.casefold()
    final_status = "LOCK_TIMEOUT" if lock_timeout else (
        "FINISHED" if str(getattr(result, "status", "")).casefold() == "success" else "ERROR"
    )
    if lock_timeout:
        _write_timeout_stats(
            self,
            spec,
            status="LOCK_TIMEOUT",
            details=details,
            hard_timeout_seconds=hard_timeout_seconds,
            lock_timeout_seconds=lock_timeout_seconds,
        )
    _write_watchdog(
        self,
        {
            **base_state,
            "status": final_status,
            "finished_at": _iso(),
            "last_error": details[:2000] if final_status != "FINISHED" else None,
        },
    )
    return result


# Patch the common stage-4 base once.  The v2 generation/migration fetcher subclasses
# this class, so it automatically receives bounded source-lock waits and the hard target
# deadline without duplicating migration logic.
_BaseFetcher._spec = _bounded_spec
_BaseFetcher._run_syncer = _watched_run_syncer
