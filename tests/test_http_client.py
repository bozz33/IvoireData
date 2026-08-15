from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests

from ivoiredata.http_client import (
    BudgetedSession,
    HttpBudget,
    HttpBudgetExceeded,
    HttpPolicy,
    HttpRunContext,
    new_session,
)
from ivoiredata import http_requests_runtime


def _context(tmp_path, *, budget: HttpBudget | None = None, policy: HttpPolicy | None = None):
    return HttpRunContext(
        source_id="civ_test",
        run_id="run-test",
        state_dir=tmp_path,
        user_agent="IvoireData-test",
        policy=policy or HttpPolicy(retries=2),
        budget=budget or HttpBudget(max_requests=10, max_bytes=1024, max_seconds=60, max_failures=2),
    )


def _response(url: str, status: int, body: bytes = b"") -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = url
    response._content = body
    response._content_consumed = True
    response.raw = SimpleNamespace(retries=SimpleNamespace(history=()))
    return response


def test_request_budget_trips_before_extra_network_work(tmp_path):
    ctx = _context(
        tmp_path,
        budget=HttpBudget(max_requests=1, max_bytes=100, max_seconds=60, max_failures=2),
    )
    ctx.before_request("https://example.test/one")
    with pytest.raises(HttpBudgetExceeded, match="request budget"):
        ctx.before_request("https://example.test/two")
    assert ctx.snapshot()["logical_requests"] == 1
    assert ctx.snapshot()["budget_exceeded"] is True


def test_byte_budget_trips_during_stream_accounting(tmp_path):
    ctx = _context(
        tmp_path,
        budget=HttpBudget(max_requests=5, max_bytes=4, max_seconds=60, max_failures=2),
    )
    ctx.consume_bytes(4)
    with pytest.raises(HttpBudgetExceeded, match="byte budget"):
        ctx.consume_bytes(1)
    assert ctx.snapshot()["bytes_downloaded"] == 4


def test_404_is_not_failure_budget_but_304_is_counted(tmp_path):
    ctx = _context(tmp_path)
    ctx.after_response(_response("https://example.test/missing", 404, b"missing"))
    ctx.after_response(_response("https://example.test/cached", 304))
    metrics = ctx.snapshot()
    assert metrics["failures"] == 0
    assert metrics["responses_304"] == 1
    assert metrics["status_counts"] == {"304": 1, "404": 1}


def test_retry_adapter_is_shared_policy(tmp_path):
    policy = HttpPolicy(retries=3, backoff_factor=0.25, retry_statuses=(429, 503))
    ctx = _context(tmp_path, policy=policy)
    session = BudgetedSession(ctx)
    retry = session.get_adapter("https://example.test").max_retries
    assert retry.total == 3
    assert 429 in retry.status_forcelist
    assert 503 in retry.status_forcelist
    assert "GET" in retry.allowed_methods
    assert "POST" not in retry.allowed_methods
    session.close()


def test_legacy_requests_session_is_instrumented_inside_run(tmp_path, monkeypatch):
    http_requests_runtime.install_requests_runtime()

    def fake_request(session, method, url, **kwargs):
        assert kwargs["timeout"] == (10.0, 180.0)
        return _response(url, 200, b"hello")

    monkeypatch.setattr(http_requests_runtime, "_ORIGINAL_SESSION_REQUEST", fake_request)
    ctx = _context(tmp_path)
    with ctx:
        with requests.Session() as session:
            response = session.get("https://example.test/data")
            assert response.content == b"hello"
    metrics = ctx.snapshot()
    assert metrics["logical_requests"] == 1
    assert metrics["network_attempts"] == 1
    assert metrics["bytes_downloaded"] == 5
    assert metrics["host_requests"] == {"example.test": 1}
    checkpoint = json.loads(ctx.checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["logical_requests"] == 1
    assert checkpoint["status"] == "FINISHED"


def test_checkpoint_is_crash_readable_and_standalone_session_is_quiet(tmp_path):
    ctx = _context(tmp_path)
    ctx.before_request("https://example.test/one")
    ctx.checkpoint("RUNNING")
    payload = json.loads(ctx.checkpoint_path.read_text(encoding="utf-8"))
    assert payload["run_id"] == "run-test"
    assert payload["logical_requests"] == 1
    assert payload["status"] == "RUNNING"

    standalone = new_session("IvoireData-test")
    assert standalone.context.checkpoint_path is None
    standalone.close()
