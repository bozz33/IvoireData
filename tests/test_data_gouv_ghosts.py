from __future__ import annotations

import hashlib
from types import SimpleNamespace

from ivoiredata.artifact_ledger import ArtifactLedger
from ivoiredata.connectors.data_gouv_ci_v2 import _confirm_catalog_ghost
from ivoiredata.data_gouv_audit import data_gouv_coverage_from_catalog
from ivoiredata.upstream_state import UpstreamState


class StatusResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class DetailSession:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return StatusResponse(self.status_code)


def test_catalog_ghost_requires_three_authority_404_signals():
    session = DetailSession(404)
    evidence = _confirm_catalog_ghost(session, "dead-dataset", full_status=404, lines_status=404)
    assert evidence is not None
    assert evidence["detail_status"] == 404
    assert evidence["classification"] == "CATALOG_VISIBLE_BUT_AUTHORITY_DATASET_MISSING"
    assert len(session.calls) == 1

    assert _confirm_catalog_ghost(DetailSession(200), "live", full_status=404, lines_status=404) is None
    assert _confirm_catalog_ghost(DetailSession(404), "transient", full_status=500, lines_status=404) is None


def test_upstream_ghost_is_terminal_and_not_repairable(tmp_path):
    upstream = UpstreamState(tmp_path / "upstreams.json")
    row = upstream.mark_ghost(
        "civ_datagouv_catalog",
        "dataset:ghost",
        url="https://data.gouv.ci/data-fair/api/v1/datasets/ghost",
        signature="catalog-v1",
        evidence={"full_status": 404, "lines_status": 404, "detail_status": 404},
    )
    assert row["last_result"] == "UPSTREAM_GHOST"
    assert row["signature"] == "catalog-v1"

    ledger = ArtifactLedger(tmp_path / "artifact_ledger.sqlite3")
    try:
        ingested = ledger.ingest_upstream_row(row)
        assert ingested["status"] == "UPSTREAM_GHOST"
        assert ledger.repair_plan(source_id="civ_datagouv_catalog")["repairable_artifacts"] == 0
        audit = ledger.audit(source_id="civ_datagouv_catalog")
        assert audit["by_status"] == {"UPSTREAM_GHOST": 1}
        assert audit["claimed_physical"] == 0
    finally:
        ledger.close()


def test_data_gouv_audit_uses_retrievable_denominator_and_persistent_verification(tmp_path):
    ledger_path = tmp_path / "artifact_ledger.sqlite3"
    raw = tmp_path / "live.csv"
    raw.write_bytes(b"a\n1\n")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()

    ledger = ArtifactLedger(ledger_path)
    try:
        live_row = {
            "source_id": "civ_datagouv_catalog",
            "artifact_id": "dataset:live",
            "url": "https://data.gouv.ci/data-fair/api/v1/datasets/live/full",
            "signature": "live-v1",
            "sha256": digest,
            "size_bytes": raw.stat().st_size,
            "local_path": str(raw),
            "downloaded": True,
            "last_result": "DOWNLOADED",
        }
        ledger.ingest_upstream_row(live_row)
        assert ledger.verify(source_id="civ_datagouv_catalog")["verified"] == 1
        ledger.ingest_upstream_row({**live_row, "last_result": "UNCHANGED"})

        ledger.ingest_upstream_row({
            "source_id": "civ_datagouv_catalog",
            "artifact_id": "dataset:ghost",
            "url": "https://data.gouv.ci/data-fair/api/v1/datasets/ghost",
            "signature": "ghost-v1",
            "last_result": "UPSTREAM_GHOST",
            "http_status": 404,
        })
    finally:
        ledger.close()

    engine = SimpleNamespace(settings=SimpleNamespace(artifact_ledger_path=ledger_path))
    payload = data_gouv_coverage_from_catalog(engine, [{"id": "live"}, {"id": "ghost"}])
    assert payload["official_visible"] == 2
    assert payload["official_retrievable"] == 1
    assert payload["upstream_ghost"] == 1
    assert payload["upstream_ghost_ids"] == ["ghost"]
    assert payload["physical"] == 1
    assert payload["verified"] == 1
    assert payload["physical_coverage_retrievable"] == 1.0
    assert payload["verified_coverage_retrievable"] == 1.0
    assert payload["complete_physical"] is True
    assert payload["complete_verified"] is True


def test_remaining_real_failure_keeps_retrievable_gate_open(tmp_path):
    ledger_path = tmp_path / "artifact_ledger.sqlite3"
    ledger = ArtifactLedger(ledger_path)
    try:
        ledger.ingest_upstream_row({
            "source_id": "civ_datagouv_catalog",
            "artifact_id": "dataset:ghost",
            "last_result": "UPSTREAM_GHOST",
            "signature": "g1",
        })
        ledger.ingest_upstream_row({
            "source_id": "civ_datagouv_catalog",
            "artifact_id": "dataset:broken",
            "last_result": "ERROR",
            "error": "timeout",
        })
    finally:
        ledger.close()

    engine = SimpleNamespace(settings=SimpleNamespace(artifact_ledger_path=ledger_path))
    payload = data_gouv_coverage_from_catalog(engine, [{"id": "ghost"}, {"id": "broken"}])
    assert payload["official_retrievable"] == 1
    assert payload["failed_ids"] == ["broken"]
    assert payload["complete_physical"] is False
    assert payload["complete_verified"] is False
