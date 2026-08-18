from __future__ import annotations

from pathlib import Path

import pytest

from ivoiredata.connectors import official_docs as base
from ivoiredata.connectors import official_docs_transport as transport
from ivoiredata.deadline import deadline_remaining_seconds, hard_deadline
from ivoiredata.state_io import load_json


class _Response:
    status_code = 200
    url = "https://example.com/docs/page"
    headers = {"content-type": "text/plain"}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=256 * 1024):
        yield b"hello"


class _Session:
    def __init__(self, *, error: BaseException | None = None):
        self.error = error
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append(("GET", url, dict(kwargs)))
        if self.error is not None:
            raise self.error
        return _Response()

    def head(self, url: str, **kwargs):
        self.calls.append(("HEAD", url, dict(kwargs)))
        if self.error is not None:
            raise self.error
        return _Response()


def test_request_timeout_is_split_and_bounded(monkeypatch):
    monkeypatch.setenv("IVOIREDATA_DOCS_CONNECT_TIMEOUT", "6.1")
    monkeypatch.setenv("IVOIREDATA_DOCS_READ_TIMEOUT", "30")
    session = _Session()
    bounded = transport._BoundedSession(session, soft_stop=False)

    bounded.get("https://example.com/docs", timeout=180)

    assert session.calls[0][2]["timeout"] == (6.1, 30.0)


def test_smaller_caller_timeout_is_preserved(monkeypatch):
    monkeypatch.setenv("IVOIREDATA_DOCS_CONNECT_TIMEOUT", "6.1")
    monkeypatch.setenv("IVOIREDATA_DOCS_READ_TIMEOUT", "30")
    session = _Session()
    bounded = transport._BoundedSession(session, soft_stop=False)

    bounded.get("https://example.com/docs", timeout=(2, 3))

    assert session.calls[0][2]["timeout"] == (2.0, 3.0)


def test_hard_deadline_exposes_cooperative_remaining_budget():
    assert deadline_remaining_seconds() is None
    with hard_deadline(2, label="cooperative-test"):
        remaining = deadline_remaining_seconds()
        assert remaining is not None
        assert 0 < remaining <= 2
    assert deadline_remaining_seconds() is None


def test_discovery_stops_before_target_watchdog_when_reserve_is_reached(monkeypatch):
    monkeypatch.setenv("IVOIREDATA_DOCS_DEADLINE_RESERVE", "5")
    session = _Session()
    bounded = transport._BoundedSession(session, soft_stop=True)

    with hard_deadline(1, label="discovery-budget"):
        with pytest.raises(base.LimitExceeded) as caught:
            bounded.get("https://example.com/docs/sitemap.xml", timeout=180)

    assert caught.value.limit == 0
    assert session.calls == []


def test_active_request_records_exact_slow_url_and_effective_timeout(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("IVOIREDATA_DOCS_CONNECT_TIMEOUT", "1")
    monkeypatch.setenv("IVOIREDATA_DOCS_READ_TIMEOUT", "2")
    snapshot_dir = tmp_path / "raw"
    session = _Session(error=RuntimeError("simulated stalled socket"))

    with pytest.raises(RuntimeError, match="stalled socket"):
        transport._bounded_fetch(
            session,
            url="https://example.com/docs/slow-page",
            source_id="docs-example",
            artifact="page:https://example.com/docs/slow-page",
            upstream=None,
            snapshot_dir=snapshot_dir,
            user_agent="IvoireData-test",
            accept="text/html",
            method="DOCS_CRAWL_DISCOVERY",
            cap=1024,
            replay=False,
        )

    state = load_json(snapshot_dir / "official_docs_active_request.json", {})
    assert state["status"] == "ERROR"
    assert state["logical_method"] == "DOCS_CRAWL_DISCOVERY"
    assert state["request_url"] == "https://example.com/docs/slow-page"
    assert state["effective_timeout"] == [1.0, 2.0]
    assert state["error_type"] == "RuntimeError"


def test_package_import_keeps_bounded_transport_active():
    assert base._fetch is transport._bounded_fetch
    assert base._root is transport._bounded_root
