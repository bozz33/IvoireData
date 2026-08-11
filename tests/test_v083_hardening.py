from __future__ import annotations

import json
from pathlib import Path

from ivoiredata.connectors.data_gouv_ci_v2 import _archive_removed_tables, _discover_official
from ivoiredata.connectors.ilostat import _country_ref_rows, _ref_area_signature
from ivoiredata.models import SourceSpec, SyncResult
from ivoiredata.post_sync import cleanup_after_success
from ivoiredata.qualification import QualificationStore
from ivoiredata.runtime_control import RuntimeControl
from ivoiredata.settings import Settings
from ivoiredata.upstream_state import UpstreamState


class FakeResponse:
    def __init__(self, payload, *, url="https://data.gouv.ci/data-fair/api/v1/datasets"):
        self._payload = payload
        self.url = url
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class QueueSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None, **kwargs):
        self.calls.append({"url": url, "params": params})
        assert self.payloads, f"unexpected request {url}"
        return FakeResponse(self.payloads.pop(0), url=url)


def _datagouv_spec() -> SourceSpec:
    return SourceSpec(
        source_id="civ_datagouv_catalog", title="Data.gouv", domain="multidomain",
        provider="Data.gouv.ci", source_url="https://data.gouv.ci/datasets",
        rights_tier="C_PUBLIC_LOCAL_INGEST", access_tier="OPEN_PUBLIC", priority="P0",
        connector="data_gouv_ci", auto_sync=True,
    )


def test_datafair_catalog_does_not_stop_when_server_caps_page_below_requested_size():
    session = QueueSession([
        {"count": 3, "results": [{"id": "a"}]},
        {"count": 3, "results": [{"id": "b"}]},
        {"count": 3, "results": [{"id": "c"}]},
    ])
    rows = _discover_official(session, page_size=1000)
    assert [row["id"] for row in rows] == ["a", "b", "c"]
    assert [call["params"]["page"] for call in session.calls] == [1, 2, 3]


def test_datafair_catalog_detects_repeated_page_before_advertised_count():
    session = QueueSession([
        {"count": 2, "results": [{"id": "a"}]},
        {"count": 2, "results": [{"id": "a"}]},
    ])
    import pytest
    with pytest.raises(RuntimeError, match="pagination stalled"):
        _discover_official(session, page_size=1000)


def test_removed_datagouv_table_is_archived_not_deleted(tmp_path: Path):
    raw = tmp_path / "domains" / "multidomain" / "civ_datagouv_catalog" / "raw"
    table = raw.parent / "tables" / "data" / "datagouv_old_dataset"
    table.mkdir(parents=True)
    (table / "part.parquet").write_bytes(b"history")
    archived = _archive_removed_tables(raw, ["old-dataset"])
    assert not table.exists()
    assert len(archived) == 1
    target = Path(archived[0]["archive_path"])
    assert (target / "part.parquet").read_bytes() == b"history"
    assert list((raw / "legacy" / "removed_upstream").rglob("archive.json"))


def test_post_sync_archives_untracked_legacy_datagouv_orphan(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "lake", state_dir=tmp_path / "state")
    spec = _datagouv_spec()
    root = settings.data_dir / "domains" / "multidomain" / spec.source_id
    tables = root / "tables" / "data"
    active = tables / "datagouv_active"
    orphan = tables / "datagouv_old_legacy"
    active.mkdir(parents=True)
    orphan.mkdir(parents=True)
    (active / "part.parquet").write_bytes(b"active")
    (orphan / "part.parquet").write_bytes(b"old")

    UpstreamState(settings.upstream_state_path).mark_unchanged(
        spec.source_id, "dataset:active", signature="v1", url="https://data.gouv.ci/datasets/active"
    )
    result = cleanup_after_success(settings, spec)
    assert result and result["archived"] == 1
    assert active.exists()
    assert not orphan.exists()
    archive = Path(result["tables"][0]["archive_path"])
    assert (archive / "part.parquet").read_bytes() == b"old"


def test_upstream_state_concurrent_instances_do_not_lose_each_other(tmp_path: Path):
    path = tmp_path / "upstreams.json"
    first = UpstreamState(path)
    second = UpstreamState(path)
    first.mark_downloaded(
        "source_a", "artifact", url="https://a", signature="v1",
        sha256="a", size_bytes=1, method="TEST",
    )
    second.mark_downloaded(
        "source_b", "artifact", url="https://b", signature="v1",
        sha256="b", size_bytes=1, method="TEST",
    )
    final = UpstreamState(path)
    assert final.get("source_a", "artifact")["sha256"] == "a"
    assert final.get("source_b", "artifact")["sha256"] == "b"


def test_runtime_control_two_instances_preserve_distinct_overrides(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "lake", state_dir=tmp_path / "state")
    first = RuntimeControl(settings)
    second = RuntimeControl(settings)
    first.set_source("a", enabled=False)
    second.set_source("b", auto_sync=True)
    payload = json.loads(settings.runtime_overrides_path.read_text())
    assert payload["sources"]["a"]["enabled"] is False
    assert payload["sources"]["b"]["auto_sync"] is True


def test_qualification_two_instances_preserve_cycles(tmp_path: Path):
    path = tmp_path / "qualification.json"
    first = QualificationStore(path)
    second = QualificationStore(path)
    first.start(["baseline"])
    first.record_cycle([SyncResult("a", "success", "s", "f", "x")])
    second.record_cycle([SyncResult("b", "success", "s", "f", "x")])
    status = QualificationStore(path).status()
    assert status["cycles_total"] == 2
    assert status["source_attempts"] == {"a": 1, "b": 1}


def test_ilostat_ref_area_signature_tracks_civ_frequency_versions():
    toc = [
        {"id": "CIV_A", "ref_area": "CIV", "freq": "A", "last.update": "2026-08-01", "n.records": 100},
        {"id": "CIV_Q", "ref_area": "CIV", "freq": "Q", "last.update": "2026-08-02", "n.records": 20},
        {"id": "SEN_A", "ref_area": "SEN", "freq": "A", "last.update": "2026-08-03", "n.records": 99},
    ]
    rows = _country_ref_rows(toc, "CIV", set())
    assert [row["id"] for row in rows] == ["CIV_A", "CIV_Q"]
    sig1 = _ref_area_signature(rows)
    changed = [dict(row) for row in rows]
    changed[0]["last.update"] = "2026-08-11"
    assert _ref_area_signature(changed) != sig1


def test_ilostat_ref_area_frequency_filter_is_respected():
    toc = [
        {"id": "CIV_A", "ref_area": "CIV", "freq": "A"},
        {"id": "CIV_M", "ref_area": "CIV", "freq": "M"},
    ]
    rows = _country_ref_rows(toc, "CIV", {"A"})
    assert [row["id"] for row in rows] == ["CIV_A"]
