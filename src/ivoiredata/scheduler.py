from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

from .engine import IvoireDataEngine
from .freshness import parse_time
from .models import SyncResult


def _has_pending_upstream(stats: dict) -> bool:
    """Return True when a successful dlt run still has upstream work to finish."""
    if not isinstance(stats, dict) or not stats:
        return False
    return bool(
        int(stats.get("failed") or 0) > 0
        or int(stats.get("backlog_count") or 0) > 0
        or int(stats.get("deferred_budget") or 0) > 0
        or int(stats.get("skipped_oversize") or 0) > 0
    )


def _pending_retry_ids(engine: IvoireDataEngine, attempted: set[str] | None = None) -> list[str]:
    """Find AUTO structured sources that need an early retry despite source freshness.

    Normal refresh intervals remain the primary cadence. This path exists for partial
    structured runs (for example a FAOSTAT transfer-budget backlog or a transient
    Data.gouv/ILOSTAT item failure). It prevents a successful *partial* run from waiting
    another 7/30 days before finishing its backlog.
    """
    attempted = attempted or set()
    specs = {
        spec.source_id: spec
        for spec in engine.registry.list(public_only=True, auto_only=True)
    }
    quality_rows = {row["source_id"]: row for row in engine.quality_audit()["rows"]}
    now = datetime.now(timezone.utc)
    due: list[str] = []
    engine.freshness.refresh()

    for source_id, spec in specs.items():
        if source_id in attempted:
            continue
        row = quality_rows.get(source_id, {})
        stats = row.get("upstream_stats") if isinstance(row, dict) else {}
        if not _has_pending_upstream(stats if isinstance(stats, dict) else {}):
            continue

        state = engine.freshness.data.get(source_id, {})
        last_attempt = parse_time(state.get("last_attempt") if isinstance(state, dict) else None)
        try:
            retry_hours = max(1, int(spec.options.get("partial_retry_hours", 6)))
        except (TypeError, ValueError):
            retry_hours = 6
        if last_attempt is None or now >= last_attempt + timedelta(hours=retry_hours):
            due.append(source_id)
    return sorted(due)


def _mark_partial_results(engine: IvoireDataEngine, results: list[SyncResult]) -> list[SyncResult]:
    """Reflect structured backlog/failures in scheduler qualification results.

    The data already committed by dlt remains valid and is never rolled back merely
    because another upstream artifact is pending. However, a partial run must not count
    as a perfect CI Gold automatic cycle.
    """
    if not results:
        return results
    quality_rows = {row["source_id"]: row for row in engine.quality_audit()["rows"]}
    for result in results:
        row = quality_rows.get(result.source_id, {})
        stats = row.get("upstream_stats") if isinstance(row, dict) else {}
        if result.status == "success" and _has_pending_upstream(stats if isinstance(stats, dict) else {}):
            result.status = "partial"
            result.details = (result.details + "\nIvoireData: upstream backlog/partial failure remains; early retry scheduled.").strip()
    return results


def _automatic_cycle(engine: IvoireDataEngine) -> list[SyncResult]:
    results = engine.sync_due(auto_only=True, public_only=True)
    attempted = {result.source_id for result in results}

    # Retry previous partial structured sources on a short cadence without forcing a
    # redownload. Connectors consult their official version/signature cache and only
    # transfer missing/changed artifacts.
    for source_id in _pending_retry_ids(engine, attempted):
        results.append(engine.sync(source_id, force=False))

    return _mark_partial_results(engine, results)


def run_once():
    engine = IvoireDataEngine()
    if not engine.runtime.automatic_enabled:
        return []
    results = _automatic_cycle(engine)
    engine.qualification.record_cycle(results)
    return results


def run_forever(interval: int | None = None) -> None:
    explicit_interval = int(interval) if interval is not None else None
    while True:
        engine = IvoireDataEngine()
        if engine.runtime.automatic_enabled:
            results = _automatic_cycle(engine)
            engine.qualification.record_cycle(results)
            for result in results:
                print(result)
        sleep_seconds = explicit_interval
        if sleep_seconds is None:
            env_value = os.getenv("IVOIREDATA_SCHEDULER_INTERVAL")
            sleep_seconds = int(env_value) if env_value else engine.runtime.scheduler_interval_seconds
        time.sleep(max(300, int(sleep_seconds)))


def main() -> None:
    run_forever()


if __name__ == "__main__":
    main()
