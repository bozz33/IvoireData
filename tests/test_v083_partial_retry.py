from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ivoiredata.models import SourceSpec, SyncResult
from ivoiredata.scheduler import _has_pending_upstream, _mark_partial_results, _pending_retry_ids


class _Registry:
    def __init__(self, specs):
        self.specs = specs

    def list(self, public_only=False, auto_only=False):
        rows = list(self.specs)
        if public_only:
            rows = [row for row in rows if row.public]
        if auto_only:
            rows = [row for row in rows if row.auto_sync]
        return rows


class _Freshness:
    def __init__(self, data):
        self.data = data

    def refresh(self):
        return None


class _Engine:
    def __init__(self, specs, quality_rows, freshness):
        self.registry = _Registry(specs)
        self._quality_rows = quality_rows
        self.freshness = _Freshness(freshness)

    def quality_audit(self):
        return {"rows": self._quality_rows}


def _spec(source_id="civ_faostat", **options):
    return SourceSpec(
        source_id=source_id,
        title=source_id,
        domain="agriculture",
        provider="test",
        source_url="https://example.test",
        rights_tier="A_OPEN_REDISTRIBUTABLE",
        access_tier="OPEN_PUBLIC",
        priority="P0",
        connector="faostat_country",
        auto_sync=True,
        options=options,
    )


def test_pending_upstream_detects_failures_and_backlogs():
    assert _has_pending_upstream({"failed": 1})
    assert _has_pending_upstream({"backlog_count": 2})
    assert _has_pending_upstream({"deferred_budget": 1})
    assert _has_pending_upstream({"skipped_oversize": 1})
    assert not _has_pending_upstream({"failed": 0, "backlog_count": 0})


def test_partial_retry_is_due_before_normal_refresh():
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat().replace("+00:00", "Z")
    engine = _Engine(
        [_spec(partial_retry_hours=6)],
        [{"source_id": "civ_faostat", "upstream_stats": {"backlog_count": 3}}],
        {"civ_faostat": {"last_attempt": old, "last_success": old}},
    )
    assert _pending_retry_ids(engine) == ["civ_faostat"]


def test_partial_retry_waits_for_short_retry_window():
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    engine = _Engine(
        [_spec(partial_retry_hours=6)],
        [{"source_id": "civ_faostat", "upstream_stats": {"backlog_count": 3}}],
        {"civ_faostat": {"last_attempt": recent}},
    )
    assert _pending_retry_ids(engine) == []


def test_partial_result_does_not_qualify_as_success():
    engine = _Engine(
        [_spec()],
        [{"source_id": "civ_faostat", "upstream_stats": {"failed": 1}}],
        {},
    )
    result = SyncResult("civ_faostat", "success", "s", "f", "faostat_country", "ok")
    rows = _mark_partial_results(engine, [result])
    assert rows[0].status == "partial"
    assert "early retry" in rows[0].details


def test_completed_result_stays_success():
    engine = _Engine(
        [_spec()],
        [{"source_id": "civ_faostat", "upstream_stats": {"failed": 0, "backlog_count": 0}}],
        {},
    )
    result = SyncResult("civ_faostat", "success", "s", "f", "faostat_country", "ok")
    rows = _mark_partial_results(engine, [result])
    assert rows[0].status == "success"
