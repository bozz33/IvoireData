from __future__ import annotations

from types import SimpleNamespace

import requests

from ivoiredata import artifact_runtime, http_requests_runtime
from ivoiredata.artifact_ledger import ArtifactLedger


class _Registry:
    def __init__(self, spec):
        self.spec = spec

    def get(self, source_id):
        assert source_id == self.spec.source_id
        return self.spec


class _Upstreams:
    def source_rows(self, source_id):
        return []


def _response(url: str) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response._content = b"ok"
    response._content_consumed = True
    response.raw = SimpleNamespace(retries=SimpleNamespace(history=()))
    return response


def test_budget_exhaustion_is_journaled_as_partial_budget(tmp_path, monkeypatch):
    spec = SimpleNamespace(
        source_id="civ_budget_test",
        connector="public_web",
        options={"http_budget": {"max_requests": 1, "max_bytes": 100, "max_seconds": 60, "max_failures": 2}},
    )
    settings = SimpleNamespace(
        artifact_ledger_path=tmp_path / "artifact_ledger.sqlite3",
        state_dir=tmp_path / "state",
        user_agent="IvoireData-test",
    )
    engine = SimpleNamespace(settings=settings, registry=_Registry(spec), upstreams=_Upstreams())

    def fake_request(session, method, url, **kwargs):
        return _response(url)

    monkeypatch.setattr(http_requests_runtime, "_ORIGINAL_SESSION_REQUEST", fake_request)

    def fake_sync(self, source_id, force=False):
        try:
            with requests.Session() as session:
                session.get("https://example.test/one")
                session.get("https://example.test/two")
        except Exception as exc:
            return SimpleNamespace(status="error", details=str(exc))
        raise AssertionError("request budget should have interrupted the second request")

    monkeypatch.setattr(artifact_runtime, "_ORIGINAL_SYNC", fake_sync)
    result = artifact_runtime._sync_with_artifact_ledger(engine, "civ_budget_test", force=True)
    assert result.status == "error"

    ledger = ArtifactLedger(settings.artifact_ledger_path)
    try:
        run = ledger.audit(source_id="civ_budget_test")["recent_runs"][0]
        assert run["status"] == "PARTIAL_BUDGET"
        assert run["budget_exceeded"] == 1
        assert run["http_requests"] == 1
        assert run["http_attempts"] == 1
        assert "request budget exceeded" in run["budget_reason"]
    finally:
        ledger.close()
