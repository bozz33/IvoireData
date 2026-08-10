from __future__ import annotations

import json
from pathlib import Path

from ivoiredata.connectors.data_gouv_ci_v2 import _discover_official, _lines_download_official
from ivoiredata.connectors.faostat import _catalog_rows, _file_size_bytes, _signature as fao_signature, _table_name
from ivoiredata.connectors.ilostat import _fetch_indicator
from ivoiredata.state_io import atomic_write_json, load_json
from ivoiredata.upstream_state import UpstreamState


class FakeResponse:
    def __init__(self, payload=None, *, url="https://example.test", status_code=200, content=None, headers=None):
        self._payload = payload
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content if content is not None else json.dumps(payload or {}).encode()
        self.text = self.content.decode("utf-8", "replace")

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err


class QueueSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None, **kwargs):
        self.calls.append({"url": url, "params": params, "timeout": timeout, "headers": headers or {}})
        assert self.responses, f"unexpected request {url}"
        return self.responses.pop(0)


def test_atomic_json_corruption_is_quarantined(tmp_path: Path):
    path = tmp_path / "freshness.json"
    atomic_write_json(path, {"ok": 1})
    assert load_json(path, {}) == {"ok": 1}
    path.write_text('{"broken":', encoding="utf-8")
    assert load_json(path, {"clean": True}) == {"clean": True}
    assert not path.exists()
    assert list(tmp_path.glob("freshness.json.corrupt-*"))


def test_upstream_state_builds_conditional_headers_and_cache(tmp_path: Path):
    state = UpstreamState(tmp_path / "upstreams.json")
    payload = tmp_path / "payload.csv"
    payload.write_text("a\n1\n", encoding="utf-8")
    state.mark_downloaded(
        "source", "artifact", url="https://example.test/data", signature="v1",
        sha256="abc", size_bytes=4, etag='"etag-v1"', last_modified="Mon, 10 Aug 2026 20:00:00 GMT",
        method="TEST", local_path=str(payload),
    )
    assert state.signature_matches("source", "artifact", "v1")
    assert state.conditional_headers("source", "artifact") == {
        "If-None-Match": '"etag-v1"',
        "If-Modified-Since": "Mon, 10 Aug 2026 20:00:00 GMT",
    }
    assert state.cached_path("source", "artifact", "v1") == payload


def test_datafair_catalog_pagination_starts_at_one():
    session = QueueSession([
        FakeResponse({"count": 3, "results": [{"id": "a"}, {"id": "b"}]}, url="https://x/datasets?page=1"),
        FakeResponse({"count": 3, "results": [{"id": "c"}]}, url="https://x/datasets?page=2"),
    ])
    rows = _discover_official(session, page_size=2)
    assert [row["id"] for row in rows] == ["a", "b", "c"]
    assert session.calls[0]["params"] == {"size": 2, "page": 1}
    assert session.calls[1]["params"] == {"size": 2, "page": 2}


def test_datafair_lines_follows_official_next_cursor(tmp_path: Path):
    session = QueueSession([
        FakeResponse(
            {"total": 3, "results": [{"id": 1}, {"id": 2}], "next": "/data-fair/api/v1/datasets/demo/lines?after=2&size=2"},
            url="https://data.gouv.ci/data-fair/api/v1/datasets/demo/lines?size=2&page=1&count=exact",
        ),
        FakeResponse(
            {"total": 3, "results": [{"id": 3}]},
            url="https://data.gouv.ci/data-fair/api/v1/datasets/demo/lines?after=2&size=2",
        ),
    ])
    rows, snapshot, _ = _lines_download_official(
        session, "demo", snapshot_dir=tmp_path, source_id="civ_datagouv_catalog", page_size=2
    )
    assert [row["id"] for row in rows] == [1, 2, 3]
    assert session.calls[0]["params"]["page"] == 1
    assert session.calls[1]["params"] is None
    assert "after=2" in session.calls[1]["url"]
    assert snapshot and Path(str(snapshot["local_path"])).exists()


def test_ilostat_indicator_request_has_id_and_ref_area():
    csv_bytes = b"ref_area,indicator,obs_value\nCIV,EMP_TEST,12\nSEN,EMP_TEST,9\n"
    session = QueueSession([FakeResponse(None, url="https://rplumber.ilo.org/data/indicator/?id=EMP_TEST", content=csv_bytes)])
    _, rows = _fetch_indicator(session, "EMP_TEST", "CIV", data_api="https://rplumber.ilo.org/data/indicator/")
    params = session.calls[0]["params"]
    assert params["id"] == "EMP_TEST"
    assert params["ref_area"] == "CIV"
    assert params["format"] == ".csv"
    assert len(rows) == 1 and rows[0]["ref_area"] == "CIV"


def test_faostat_official_catalog_helpers_are_stable():
    payload = {"Datasets": {"Dataset": [
        {"DatasetCode": "QCL", "DatasetName": "Production", "DateUpdate": "2026-08-01", "FileSize": "25MB", "FileRows": 100, "FileLocation": "https://example/qcl.zip"}
    ]}}
    rows = _catalog_rows(payload)
    assert rows[0]["DatasetCode"] == "QCL"
    assert _file_size_bytes("25MB") == 25 * 1024 * 1024
    assert _table_name("QCL") == "faostat_production_crops_livestock"
    assert fao_signature(rows[0]) == fao_signature(dict(rows[0]))
    changed = dict(rows[0]); changed["DateUpdate"] = "2026-08-02"
    assert fao_signature(rows[0]) != fao_signature(changed)
