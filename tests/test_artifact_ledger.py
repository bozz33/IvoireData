from __future__ import annotations

import hashlib
import sqlite3

from ivoiredata.artifact_ledger import ArtifactLedger


def test_artifact_ledger_detects_verified_corrupted_and_missing(tmp_path):
    database = tmp_path / "state" / "artifact_ledger.sqlite3"
    raw = tmp_path / "raw" / "dataset.csv"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"a,b\n1,2\n")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()

    ledger = ArtifactLedger(database)
    try:
        upstream_row = {
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
        row = ledger.ingest_upstream_row(upstream_row)
        assert row["status"] == "FETCHED"
        assert row["verification_status"] == "UNVERIFIED"

        verified = ledger.verify(source_id="civ_test")
        assert verified["verified"] == 1
        verified_row = ledger.get("civ_test", "dataset:one")
        assert verified_row["status"] == "FETCHED"
        assert verified_row["verification_status"] == "VERIFIED"
        assert verified_row["verified_sha256"] == digest
        verified_at = verified_row["verified_at"]

        # A later unchanged sync must not erase cryptographic proof for the same bytes.
        ledger.ingest_upstream_row({**upstream_row, "last_result": "UNCHANGED"})
        unchanged = ledger.get("civ_test", "dataset:one")
        assert unchanged["status"] == "UNCHANGED"
        assert unchanged["verification_status"] == "VERIFIED"
        assert unchanged["verified_at"] == verified_at
        assert ledger.audit(source_id="civ_test")["verified_artifacts"] == 1

        raw.write_bytes(b"tampered")
        corrupted = ledger.verify(source_id="civ_test")
        assert corrupted["corrupted"] == 1
        corrupted_row = ledger.get("civ_test", "dataset:one")
        assert corrupted_row["status"] == "CORRUPTED"
        assert corrupted_row["verification_status"] == "FAILED"
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


def test_artifact_run_ledger_records_observed_artifacts_and_http_metrics(tmp_path):
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
        ledger.finish_run(
            run_id,
            status="SUCCESS",
            http_metrics={
                "logical_requests": 3,
                "network_attempts": 4,
                "retries": 1,
                "responses_304": 1,
                "bytes_downloaded": 1234,
                "failures": 0,
                "rate_limit_wait_seconds": 0.25,
                "elapsed_seconds": 1.5,
                "budget_exceeded": False,
                "status_counts": {"200": 2, "304": 1},
            },
        )
        audit = ledger.audit(source_id="civ_test")
        assert audit["schema_version"] == 3
        run = audit["recent_runs"][0]
        assert run["run_id"] == run_id
        assert run["status"] == "SUCCESS"
        assert run["artifacts_observed"] == 1
        assert run["bytes_observed"] == raw.stat().st_size
        assert run["http_requests"] == 3
        assert run["http_attempts"] == 4
        assert run["http_retries"] == 1
        assert run["http_304"] == 1
        assert run["http_bytes"] == 1234
        assert run["budget_exceeded"] == 0
        assert '"304": 1' in run["http_metrics_json"]
    finally:
        ledger.close()


def test_v1_migration_recovers_verification_and_adds_run_metrics(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    raw = tmp_path / "legacy.csv"
    raw.write_bytes(b"x\n1\n")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    db = sqlite3.connect(database)
    try:
        db.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO schema_meta(key,value) VALUES('schema_version','1')")
        db.executescript(
            """
            CREATE TABLE artifacts (
                source_id TEXT NOT NULL, artifact_id TEXT NOT NULL, upstream_id TEXT,
                upstream_url TEXT, artifact_type TEXT, status TEXT NOT NULL DEFAULT 'DISCOVERED',
                upstream_signature TEXT, etag TEXT, last_modified TEXT, sha256 TEXT,
                size_bytes INTEGER, local_path TEXT, first_seen_at TEXT NOT NULL,
                last_checked_at TEXT, downloaded_at TEXT, verified_at TEXT,
                http_status INTEGER, fetch_method TEXT, error TEXT, last_run_id TEXT,
                PRIMARY KEY (source_id, artifact_id)
            );
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, connector TEXT, force INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'RUNNING', started_at TEXT NOT NULL, finished_at TEXT,
                error TEXT, artifacts_observed INTEGER NOT NULL DEFAULT 0, bytes_observed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE run_artifacts (
                run_id TEXT NOT NULL, source_id TEXT NOT NULL, artifact_id TEXT NOT NULL,
                status TEXT NOT NULL, size_bytes INTEGER, local_path TEXT, observed_at TEXT NOT NULL,
                PRIMARY KEY (run_id, source_id, artifact_id)
            );
            """
        )
        db.execute(
            """
            INSERT INTO artifacts(
                source_id,artifact_id,status,sha256,size_bytes,local_path,first_seen_at,verified_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                "civ_datagouv_catalog", "dataset:legacy", "UNCHANGED", digest,
                raw.stat().st_size, str(raw), "2026-08-15T00:00:00Z", "2026-08-15T01:00:00Z",
            ),
        )
        db.commit()
    finally:
        db.close()

    ledger = ArtifactLedger(database)
    try:
        row = ledger.get("civ_datagouv_catalog", "dataset:legacy")
        assert ledger.schema_version == 3
        assert row["status"] == "UNCHANGED"
        assert row["verification_status"] == "VERIFIED"
        assert row["verified_sha256"] == digest
        run_columns = {
            item["name"] for item in ledger.db.execute("PRAGMA table_info(runs)").fetchall()
        }
        assert {"http_requests", "http_bytes", "budget_exceeded", "http_metrics_json"} <= run_columns
        assert ledger.audit(source_id="civ_datagouv_catalog")["verified_artifacts"] == 1
    finally:
        ledger.close()
