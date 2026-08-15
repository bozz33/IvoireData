from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ivoiredata.artifact_ledger import ArtifactLedger
from ivoiredata.connectors.data_gouv_ci_v2 import (
    _full_download_streaming,
    _has_physical_cache,
    _iter_csv_path,
    _iter_ndjson_path,
    _lines_download_streaming,
)
from ivoiredata.data_gouv_audit import data_gouv_coverage_from_catalog
from ivoiredata.upstream_state import UpstreamState


class FakeResponse:
    def __init__(self, *, url: str, body: bytes = b"", payload=None, content_type: str = "text/csv"):
        self.url = url
        self._body = body
        self._payload = payload
        self.headers = {"content-type": content_type}
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload

    def iter_content(self, chunk_size: int):
        for start in range(0, len(self._body), max(1, chunk_size // 3)):
            yield self._body[start:start + max(1, chunk_size // 3)]


class FullSession:
    def __init__(self, response: FakeResponse):
        self.response = response

    def get(self, url, **kwargs):
        return self.response


class LinesSession:
    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return FakeResponse(
                url="https://data.gouv.ci/data-fair/api/v1/datasets/demo/lines?page=1",
                payload={"rows": [{"a": 1}, {"a": 2}], "next": "?page=2"},
                content_type="application/json",
            )
        return FakeResponse(
            url="https://data.gouv.ci/data-fair/api/v1/datasets/demo/lines?page=2",
            payload={"rows": [{"a": 3}]},
            content_type="application/json",
        )


def test_full_download_is_streamed_to_disk(tmp_path):
    body = b"annee;valeur\n2024;12\n2025;13\n"
    response = FakeResponse(
        url="https://data.gouv.ci/data-fair/api/v1/datasets/demo/full",
        body=body,
        content_type="text/csv",
    )
    materialized = _full_download_streaming(
        FullSession(response), "demo", snapshot_dir=tmp_path, source_id="civ_datagouv_catalog"
    )
    assert materialized.path.is_file()
    assert materialized.snapshot["size_bytes"] == len(body)
    assert list(_iter_csv_path(materialized.path)) == [
        {"annee": "2024", "valeur": "12"},
        {"annee": "2025", "valeur": "13"},
    ]
    assert materialized.method == "FULL_STREAM"


def test_lines_follow_next_and_write_ndjson(tmp_path):
    session = LinesSession()
    materialized = _lines_download_streaming(
        session, "demo", snapshot_dir=tmp_path, source_id="civ_datagouv_catalog", page_size=2
    )
    assert session.calls == 2
    assert materialized.method == "LINES_STREAM"
    assert materialized.row_count_hint == 3
    assert list(_iter_ndjson_path(materialized.path)) == [{"a": 1}, {"a": 2}, {"a": 3}]


def test_dlt_signature_without_raw_file_is_not_physical_truth(tmp_path):
    upstream = UpstreamState(tmp_path / "upstreams.json")
    signature = "abc123"
    upstream.mark_downloaded(
        "civ_datagouv_catalog",
        "dataset:demo",
        url="https://data.gouv.ci/datasets/demo",
        signature=signature,
        sha256=None,
        size_bytes=None,
        method="ADOPTED_LEGACY_SIGNATURE",
        local_path=None,
    )
    assert not _has_physical_cache(upstream, "civ_datagouv_catalog", "dataset:demo", signature)

    raw = tmp_path / "demo.csv"
    raw.write_text("a\n1\n", encoding="utf-8")
    upstream.mark_downloaded(
        "civ_datagouv_catalog",
        "dataset:demo",
        url="https://data.gouv.ci/data-fair/api/v1/datasets/demo/full",
        signature=signature,
        sha256=None,
        size_bytes=raw.stat().st_size,
        method="FULL_STREAM",
        local_path=str(raw),
    )
    assert _has_physical_cache(upstream, "civ_datagouv_catalog", "dataset:demo", signature)


def test_physical_coverage_reports_exact_missing_and_failed_ids(tmp_path):
    ledger_path = tmp_path / "artifact_ledger.sqlite3"
    ledger = ArtifactLedger(ledger_path)
    physical = tmp_path / "a.csv"
    physical.write_text("x\n1\n", encoding="utf-8")
    ledger.ingest_upstream_row({
        "source_id": "civ_datagouv_catalog",
        "artifact_id": "dataset:a",
        "downloaded": True,
        "last_result": "DOWNLOADED",
        "local_path": str(physical),
        "size_bytes": physical.stat().st_size,
    })
    ledger.ingest_upstream_row({
        "source_id": "civ_datagouv_catalog",
        "artifact_id": "dataset:b",
        "last_result": "ERROR",
        "error": "boom",
    })
    ledger.close()

    engine = SimpleNamespace(settings=SimpleNamespace(artifact_ledger_path=ledger_path))
    payload = data_gouv_coverage_from_catalog(
        engine,
        [{"id": "a"}, {"id": "b"}, {"id": "c"}],
    )
    assert payload["official_visible"] == 3
    assert payload["physical"] == 1
    assert payload["failed_ids"] == ["b"]
    assert payload["missing_ids"] == ["c"]
    assert not payload["complete_physical"]
