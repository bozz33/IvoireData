from __future__ import annotations

import hashlib

from ivoiredata.artifact_ledger import ArtifactLedger


def test_artifact_ledger_detects_verified_corrupted_and_missing(tmp_path):
    database = tmp_path / "state" / "artifact_ledger.sqlite3"
    raw = tmp_path / "raw" / "dataset.csv"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"a,b\n1,2\n")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()

    ledger = ArtifactLedger(database)
    try:
        row = ledger.ingest_upstream_row(
            {
                "source_id": "civ_test",
                "artifact_id": "dataset:one",
                "url": "https://example.test/one.csv",
                "signature": "v1",
                "sha256": digest,
                "size_bytes": raw.stat().st_size,
                "local_path": str(raw),
                "downloaded": True,
                "last_result": "DOWNLOADED",
                "last_downloaded": "2026-08-15T00:00:00Z",
            }
        )
        assert row["status"] == "FETCHED"

        verified = ledger.verify(source_id="civ_test")
        assert verified["verified"] == 1
        assert ledger.get("civ_test", "dataset:one")["status"] == "VERIFIED"

        raw.write_bytes(b"tampered")
        corrupted = ledger.verify(source_id="civ_test")
        assert corrupted["corrupted"] == 1
        assert ledger.get("civ_test", "dataset:one")["status"] == "CORRUPTED"
        assert ledger.repair_plan(source_id="civ_test")["repairable_artifacts"] == 1

        raw.unlink()
        missing = ledger.verify(source_id="civ_test")
        assert missing["local_missing"] == 1
        assert ledger.get("civ_test", "dataset:one")["status"] == "LOCAL_MISSING"
    finally:
        ledger.close()


def test_artifact_ledger_never_claims_fetched_without_local_file(tmp_path):
    ledger = ArtifactLedger(tmp_path / "ledger.sqlite3")
    try:
        row = ledger.ingest_upstream_row(
            {
                "source_id": "civ_test",
                "artifact_id": "dataset:missing",
                "url": "https://example.test/missing.csv",
                "signature": "v1",
                "downloaded": True,
                "last_result": "UNCHANGED",
            }
        )
        assert row["status"] == "LOCAL_MISSING"
        audit = ledger.audit(source_id="civ_test")
        assert audit["by_status"] == {"LOCAL_MISSING": 1}
        assert audit["claimed_physical"] == 0
    finally:
        ledger.close()


def test_artifact_run_ledger_records_observed_artifacts(tmp_path):
    raw = tmp_path / "artifact.bin"
    raw.write_bytes(b"hello")
    ledger = ArtifactLedger(tmp_path / "ledger.sqlite3")
    try:
        run_id = ledger.start_run("civ_test", connector="http_file", force=True)
        ledger.ingest_upstream_row(
            {
                "source_id": "civ_test",
                "artifact_id": "file:main",
                "url": "https://example.test/file.bin",
                "local_path": str(raw),
                "size_bytes": raw.stat().st_size,
                "sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
                "downloaded": True,
                "last_result": "DOWNLOADED",
            },
            run_id=run_id,
        )
        ledger.finish_run(run_id, status="SUCCESS")
        audit = ledger.audit(source_id="civ_test")
        assert audit["schema_version"] == 1
        assert audit["recent_runs"][0]["run_id"] == run_id
        assert audit["recent_runs"][0]["status"] == "SUCCESS"
        assert audit["recent_runs"][0]["artifacts_observed"] == 1
        assert audit["recent_runs"][0]["bytes_observed"] == raw.stat().st_size
    finally:
        ledger.close()
