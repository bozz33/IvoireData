from __future__ import annotations

import json
from pathlib import Path

import pytest

from ivoiredata.technology_harvester import (
    HarvestCandidate,
    NPM_BOOTSTRAP_SOURCE,
    NPM_CHANGES_SOURCE,
    RegistryHarvester,
    TechnologyHarvestQueue,
)


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses.pop(0)


def _bootstrap_state(queue: TechnologyHarvestQueue):
    raw = queue.cursor(NPM_BOOTSTRAP_SOURCE).get("cursor")
    return json.loads(raw) if raw else {}


def test_npm_incremental_refuses_to_fake_global_coverage_before_bootstrap(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    session = FakeSession([
        FakeResponse({"db_name": "registry", "doc_count": 3_500_000, "update_seq": 61_000_000})
    ])
    try:
        result = RegistryHarvester(queue=queue, user_agent="test", session=session).harvest("npm", limit=10)
        assert result["bootstrap_required"] is True
        assert result["current_update_seq"] == "61000000"
        assert result["current_doc_count"] == 3_500_000
        assert result["discovered"] == 0
        assert queue.cursor(NPM_CHANGES_SOURCE) == {}
    finally:
        queue.close()


def test_npm_full_bootstrap_is_bounded_resumable_and_enables_changes_only_at_end(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    first_session = FakeSession([
        FakeResponse({"db_name": "registry", "doc_count": 3, "update_seq": 100}),
        FakeResponse({
            "total_rows": 3,
            "rows": [
                {"id": "@scope/a", "key": "@scope/a", "value": {"rev": "1-a"}},
                {"id": "b", "key": "b", "value": {"rev": "1-b"}},
            ],
        }),
    ])
    try:
        first = RegistryHarvester(queue=queue, user_agent="test", session=first_session).harvest(
            "npm", full=True, limit=2
        )
        assert first["complete"] is False
        assert first["discovered"] == 2
        assert first["processed_rows"] == 2
        assert first["snapshot_seq"] == "100"
        assert queue.cursor(NPM_CHANGES_SOURCE) == {}
        state = _bootstrap_state(queue)
        assert state["startkey"] == "b"
        assert state["complete"] is False
        pending = {row["name"] for row in queue.pending(10)}
        assert pending == {"@scope/a", "b"}

        second_session = FakeSession([
            FakeResponse({
                "total_rows": 3,
                "rows": [
                    {"id": "b", "key": "b", "value": {"rev": "1-b"}},
                    {"id": "c", "key": "c", "value": {"rev": "1-c"}},
                ],
            })
        ])
        second = RegistryHarvester(queue=queue, user_agent="test", session=second_session).harvest(
            "npm", full=True, limit=10
        )
        assert second["complete"] is True
        assert second["discovered"] == 1
        assert second["inserted"] == 1
        assert queue.cursor(NPM_CHANGES_SOURCE)["cursor"] == "100"
        state = _bootstrap_state(queue)
        assert state["complete"] is True
        assert state["startkey"] == "c"
        assert second_session.calls[0][1]["params"]["startkey"] == '"b"'
        assert {row["name"] for row in queue.pending(10)} == {"@scope/a", "b", "c"}
    finally:
        queue.close()


def test_npm_completed_bootstrap_is_zero_network_on_repeat(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    queue.set_cursor(
        NPM_BOOTSTRAP_SOURCE,
        cursor=json.dumps({"complete": True, "snapshot_seq": "123", "startkey": "z"}),
    )
    session = FakeSession([])
    try:
        result = RegistryHarvester(queue=queue, user_agent="test", session=session).harvest(
            "npm", full=True, limit=100
        )
        assert result["complete"] is True
        assert result["discovered"] == 0
        assert queue.cursor(NPM_CHANGES_SOURCE)["cursor"] == "123"
        assert session.calls == []
    finally:
        queue.close()


def test_npm_changes_preserve_event_order_and_advance_cursor_atomically(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    queue.upsert_many([HarvestCandidate("npmjs.org", "demo", "seed", 10)])
    queue.mark_qualified("npmjs.org", "demo")
    queue.set_cursor(
        NPM_BOOTSTRAP_SOURCE,
        cursor=json.dumps({"complete": True, "snapshot_seq": "100", "startkey": "z"}),
    )
    queue.set_cursor(NPM_CHANGES_SOURCE, cursor="100")
    session = FakeSession([
        FakeResponse({
            "results": [
                {"seq": 101, "id": "demo", "changes": [{"rev": "2-a"}]},
                {"seq": 102, "id": "demo", "deleted": True, "changes": [{"rev": "3-b"}]},
                {"seq": 103, "id": "demo", "changes": [{"rev": "4-c"}]},
                {"seq": 104, "id": "gone", "deleted": True, "changes": [{"rev": "2-x"}]},
            ],
            "last_seq": 104,
            "pending": 7,
        })
    ])
    try:
        result = RegistryHarvester(queue=queue, user_agent="test", session=session).harvest("npm", limit=50)
        assert result["previous_cursor"] == "100"
        assert result["cursor"] == "104"
        assert result["results"] == 4
        assert result["unique_packages"] == 2
        assert result["deleted"] == 2
        assert result["pending"] == 7
        assert queue.cursor(NPM_CHANGES_SOURCE)["cursor"] == "104"
        pending = queue.pending(10)
        assert [row["name"] for row in pending] == ["demo"]
        assert pending[0]["status"] == "PENDING"
        deleted = queue.db.execute(
            "SELECT status FROM candidates WHERE registry='npmjs.org' AND name='gone'"
        ).fetchone()
        assert deleted["status"] == "DELETED"
    finally:
        queue.close()


def test_change_batch_rolls_back_candidate_mutations_if_cursor_write_fails(tmp_path: Path, monkeypatch):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    queue.set_cursor(NPM_CHANGES_SOURCE, cursor="10")
    try:
        original = queue._set_cursor_no_commit

        def fail_cursor(source, **kwargs):
            if source == NPM_CHANGES_SOURCE and kwargs.get("cursor") == "11":
                raise RuntimeError("simulated cursor write failure")
            return original(source, **kwargs)

        monkeypatch.setattr(queue, "_set_cursor_no_commit", fail_cursor)
        with pytest.raises(RuntimeError, match="cursor write failure"):
            queue.apply_change_events(
                registry="npmjs.org",
                source=NPM_CHANGES_SOURCE,
                events=[{"name": "should-not-stick", "deleted": False}],
                cursor="11",
            )
        assert queue.cursor(NPM_CHANGES_SOURCE)["cursor"] == "10"
        row = queue.db.execute(
            "SELECT * FROM candidates WHERE registry='npmjs.org' AND name='should-not-stick'"
        ).fetchone()
        assert row is None
    finally:
        queue.close()


def test_bootstrap_page_rolls_back_candidates_if_cursor_write_fails(tmp_path: Path, monkeypatch):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        original = queue._set_cursor_no_commit

        def fail_cursor(source, **kwargs):
            if source == NPM_BOOTSTRAP_SOURCE:
                raise RuntimeError("simulated checkpoint failure")
            return original(source, **kwargs)

        monkeypatch.setattr(queue, "_set_cursor_no_commit", fail_cursor)
        with pytest.raises(RuntimeError, match="checkpoint failure"):
            queue.upsert_many_with_cursor(
                [HarvestCandidate("npmjs.org", "atomic-package", NPM_BOOTSTRAP_SOURCE, 15)],
                source=NPM_BOOTSTRAP_SOURCE,
                cursor='{"startkey":"atomic-package"}',
            )
        assert queue.cursor(NPM_BOOTSTRAP_SOURCE) == {}
        assert queue.pending(10) == []
    finally:
        queue.close()
