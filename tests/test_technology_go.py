from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import ivoiredata.technology_go as go_module
from ivoiredata.technology_go import (
    GO_BOOTSTRAP_SOURCE,
    GO_CHANGES_SOURCE,
    GO_INDEX_URL,
    GoModuleIndexHarvester,
    _go_proxy_escape,
)
from ivoiredata.technology_harvester import TechnologyHarvestQueue
from ivoiredata.technology_registries import build_purl, native_package_metadata


class FakeResponse:
    def __init__(self, *, text="", payload=None, status_code=200):
        self.text = text
        self._payload = payload
        self.status_code = status_code
        self.headers = {}
        self.content = text.encode("utf-8")

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON payload")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses=None, routes=None):
        self.responses = list(responses or [])
        self.routes = routes or {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url in self.routes:
            value = self.routes[url]
            if isinstance(value, list):
                value = value.pop(0)
            return value
        if not self.responses:
            raise AssertionError(f"no fake response left for {url}")
        return self.responses.pop(0)


def _record(path, version, timestamp):
    return {"Path": path, "Version": version, "Timestamp": timestamp}


def _ndjson(records):
    return "\n".join(json.dumps(item) for item in records) + "\n"


def _dt(hour, minute=0):
    return datetime(2026, 8, 16, hour, minute, tzinfo=timezone.utc)


def test_go_follower_refuses_fake_head_before_bootstrap(tmp_path):
    queue = TechnologyHarvestQueue(tmp_path / "queue.sqlite3")
    session = FakeSession()
    try:
        harvester = GoModuleIndexHarvester(queue=queue, user_agent="test", session=session)
        result = harvester.changes(limit=10)
        assert result["bootstrap_required"] is True
        assert result["processed_versions"] == 0
        assert session.calls == []
        assert queue.cursor(GO_CHANGES_SOURCE) == {}
    finally:
        queue.close()


def test_go_bootstrap_and_follower_resume_same_timestamp_without_loss(tmp_path):
    snapshot_page1 = [
        _record("example.com/a", "v1.0.0", "2026-08-16T08:00:00Z"),
        _record("example.com/b", "v1.0.0", "2026-08-16T09:00:00Z"),
        _record("example.com/c", "v1.0.0", "2026-08-16T09:30:00Z"),
    ]
    snapshot_page2 = [
        _record("example.com/b", "v1.0.0", "2026-08-16T09:00:00Z"),
        _record("example.com/c", "v1.0.0", "2026-08-16T09:30:00Z"),
        _record("example.com/future", "v1.0.0", "2026-08-16T10:30:00Z"),
    ]
    delta_same_timestamp = [
        _record("example.com/e", "v1.0.0", "2026-08-16T10:15:00Z"),
        _record("example.com/f", "v1.0.0", "2026-08-16T10:15:00Z"),
    ]
    session = FakeSession(
        responses=[
            FakeResponse(text=_ndjson(snapshot_page1)),
            FakeResponse(text=_ndjson(snapshot_page2)),
            FakeResponse(text=_ndjson(delta_same_timestamp)),
            FakeResponse(text=_ndjson(delta_same_timestamp)),
        ]
    )
    times = iter([_dt(10), _dt(11)])
    queue = TechnologyHarvestQueue(tmp_path / "queue.sqlite3")
    try:
        harvester = GoModuleIndexHarvester(
            queue=queue,
            user_agent="test",
            session=session,
            now_fn=lambda: next(times),
        )

        first = harvester.bootstrap(limit=2, reset=True)
        assert first["complete"] is False
        assert first["processed_versions"] == 2
        snapshot = first["snapshot_timestamp"]
        assert snapshot == "2026-08-16T10:00:00.000000000Z"
        assert first["changes_cursor"] is None
        first_cursor = queue.cursor(GO_BOOTSTRAP_SOURCE)["cursor"]

        second = harvester.bootstrap(limit=2)
        assert second["complete"] is True
        assert second["snapshot_timestamp"] == snapshot
        assert second["processed_versions"] == 1
        assert second["registry_candidates"] == 3
        assert second["version_states"] == 3
        assert second["changes_cursor"] == snapshot
        assert queue.cursor(GO_BOOTSTRAP_SOURCE)["cursor"] != first_cursor

        # A completed bootstrap is a true zero-work fast path.
        calls_before = len(session.calls)
        rerun = harvester.bootstrap(limit=5000)
        assert rerun["complete"] is True
        assert rerun["processed_versions"] == 0
        assert rerun["pages_fetched"] == 0
        assert rerun["http_work_required"] is False
        assert len(session.calls) == calls_before

        partial = harvester.changes(limit=1)
        assert partial["target_complete"] is False
        assert partial["previous_cursor"] == snapshot
        assert partial["cursor"] == snapshot
        assert partial["processed_versions"] == 1
        assert partial["inflight"] is not None

        finish = harvester.changes(limit=10)
        assert finish["target_complete"] is True
        assert finish["previous_cursor"] == snapshot
        assert finish["cursor"] == "2026-08-16T11:00:00.000000000Z"
        assert finish["inflight"] is None
        assert finish["processed_versions"] == 1
        assert finish["registry_candidates"] == 5
        assert finish["version_states"] == 5

        # The second bootstrap request resumes at the last processed timestamp.
        assert session.calls[0][0] == GO_INDEX_URL
        assert session.calls[0][1]["params"] == {"limit": 2000, "include": "all"}
        assert session.calls[1][1]["params"]["since"] == "2026-08-16T09:00:00.000000000Z"
        # The second incremental call replays the shared timestamp and uses after_key
        # locally so example.com/f is not lost.
        assert session.calls[3][1]["params"]["since"] == "2026-08-16T10:15:00.000000000Z"
    finally:
        queue.close()


def test_go_full_page_stall_refuses_unsafe_cursor_advance(tmp_path, monkeypatch):
    monkeypatch.setattr(go_module, "GO_PAGE_LIMIT", 2)
    records = [
        _record("example.com/a", "v1.0.0", "2026-08-16T09:00:00Z"),
        _record("example.com/b", "v1.0.0", "2026-08-16T09:00:00Z"),
    ]
    session = FakeSession(responses=[FakeResponse(text=_ndjson(records))])
    queue = TechnologyHarvestQueue(tmp_path / "queue.sqlite3")
    try:
        queue.set_cursor(
            GO_BOOTSTRAP_SOURCE,
            cursor=json.dumps({
                "complete": False,
                "snapshot_timestamp": "2026-08-16T10:00:00.000000000Z",
                "since_timestamp": "2026-08-16T09:00:00.000000000Z",
                "after_key": [
                    "2026-08-16T09:00:00.000000000Z",
                    "example.com/b",
                    "v1.0.0",
                ],
            }),
        )
        harvester = GoModuleIndexHarvester(queue=queue, user_agent="test", session=session)
        with pytest.raises(RuntimeError, match="cursor stalled"):
            harvester.bootstrap(limit=100)
        # Cursor is unchanged: failure is safe/replayable.
        state = json.loads(queue.cursor(GO_BOOTSTRAP_SOURCE)["cursor"])
        assert state["after_key"][1] == "example.com/b"
    finally:
        queue.close()


def test_go_native_authority_uses_proxy_protocol_and_release_semver():
    module = "github.com/stretchr/testify"
    list_url = "https://proxy.golang.org/github.com/stretchr/testify/@v/list"
    session = FakeSession(routes={
        list_url: FakeResponse(text="v1.8.4\nv1.10.0-rc.1\nv1.9.0\nv1.10.0+incompatible\n"),
    })
    metadata = native_package_metadata(
        "proxy.golang.org",
        module,
        session=session,
        user_agent="test",
    )
    assert metadata is not None
    assert metadata["authority_source"] == "go"
    assert metadata["name"] == module
    assert metadata["latest_stable_version"] == "v1.10.0+incompatible"
    assert metadata["canonical_repository"] == "https://github.com/stretchr/testify"
    assert metadata["native_registry_url"] == list_url
    assert build_purl("proxy.golang.org", module) == "pkg:golang/github.com/stretchr/testify"


def test_go_proxy_escape_encodes_uppercase_per_protocol():
    assert _go_proxy_escape("github.com/Azure/azure-sdk-for-go") == "github.com/!azure/azure-sdk-for-go"
