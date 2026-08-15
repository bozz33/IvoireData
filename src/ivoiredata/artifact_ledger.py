from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 2
PHYSICAL_STATUSES = {"FETCHED", "VERIFIED", "UNCHANGED"}
REPAIRABLE_STATUSES = {"LOCAL_MISSING", "CORRUPTED", "FAILED"}
TERMINAL_NON_PHYSICAL = {"REMOVED", "DELETED", "EMPTY_VALID", "UPSTREAM_GHOST"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactLedger:
    """Physical truth ledger for upstream artifacts and synchronization runs.

    ``status`` describes the latest acquisition state. Verification is deliberately
    independent: an artifact may be ``UNCHANGED`` on a later sync while remaining
    cryptographically verified for the exact same local bytes.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=60000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = self.db.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"artifact ledger schema {version} is newer than supported {SCHEMA_VERSION}"
            )
        if version < 1:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    source_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    upstream_id TEXT,
                    upstream_url TEXT,
                    artifact_type TEXT,
                    status TEXT NOT NULL DEFAULT 'DISCOVERED',
                    upstream_signature TEXT,
                    etag TEXT,
                    last_modified TEXT,
                    sha256 TEXT,
                    size_bytes INTEGER,
                    local_path TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    downloaded_at TEXT,
                    verified_at TEXT,
                    http_status INTEGER,
                    fetch_method TEXT,
                    error TEXT,
                    last_run_id TEXT,
                    PRIMARY KEY (source_id, artifact_id)
                );
                CREATE INDEX IF NOT EXISTS idx_artifacts_status ON artifacts(status);
                CREATE INDEX IF NOT EXISTS idx_artifacts_source_status ON artifacts(source_id, status);
                CREATE INDEX IF NOT EXISTS idx_artifacts_sha256 ON artifacts(sha256);

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    connector TEXT,
                    force INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'RUNNING',
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error TEXT,
                    artifacts_observed INTEGER NOT NULL DEFAULT 0,
                    bytes_observed INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_runs_source_started ON runs(source_id, started_at DESC);

                CREATE TABLE IF NOT EXISTS run_artifacts (
                    run_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    size_bytes INTEGER,
                    local_path TEXT,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, source_id, artifact_id),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );
                """
            )
            version = 1

        if version < 2:
            columns = {
                str(item["name"])
                for item in self.db.execute("PRAGMA table_info(artifacts)").fetchall()
            }
            if "verification_status" not in columns:
                self.db.execute(
                    "ALTER TABLE artifacts ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED'"
                )
            if "verified_sha256" not in columns:
                self.db.execute("ALTER TABLE artifacts ADD COLUMN verified_sha256 TEXT")
            # v1 used status=VERIFIED. verified_at was intentionally not overwritten by
            # later upstream ingests, so deployed ledgers can recover this proof exactly.
            self.db.execute(
                """
                UPDATE artifacts
                SET verification_status='VERIFIED', verified_sha256=sha256
                WHERE verified_at IS NOT NULL OR status='VERIFIED'
                """
            )
            self.db.execute("UPDATE artifacts SET status='FETCHED' WHERE status='VERIFIED'")
            version = 2

        self.db.execute(
            "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
            (str(version),),
        )
        self.db.commit()

    @property
    def schema_version(self) -> int:
        row = self.db.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        return int(row["value"]) if row else 0

    def start_run(self, source_id: str, *, connector: str | None = None, force: bool = False) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{stamp}-{source_id}-{uuid.uuid4().hex[:8]}"
        self.db.execute(
            """
            INSERT INTO runs(run_id,source_id,connector,force,status,started_at)
            VALUES(?,?,?,?, 'RUNNING', ?)
            """,
            (run_id, source_id, connector, 1 if force else 0, _now()),
        )
        self.db.commit()
        return run_id

    def finish_run(self, run_id: str, *, status: str, error: str | None = None) -> None:
        counts = self.db.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS bytes
            FROM run_artifacts WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        self.db.execute(
            """
            UPDATE runs SET status=?, finished_at=?, error=?, artifacts_observed=?, bytes_observed=?
            WHERE run_id=?
            """,
            (
                status,
                _now(),
                str(error)[-2000:] if error else None,
                int(counts["n"] or 0),
                int(counts["bytes"] or 0),
                run_id,
            ),
        )
        self.db.commit()

    @staticmethod
    def _physical_state(row: dict[str, Any]) -> str:
        last_result = str(row.get("last_result") or "").upper()
        if row.get("removed") or last_result == "REMOVED_UPSTREAM":
            return "REMOVED"
        if last_result == "UPSTREAM_GHOST":
            return "UPSTREAM_GHOST"
        if last_result == "ERROR" or row.get("error"):
            return "FAILED"
        claimed = bool(row.get("downloaded")) or last_result in {"DOWNLOADED", "UNCHANGED"}
        local_value = str(row.get("local_path") or "").strip()
        if claimed:
            if not local_value or not Path(local_value).is_file():
                return "LOCAL_MISSING"
            return "UNCHANGED" if last_result == "UNCHANGED" else "FETCHED"
        return "DISCOVERED"

    def ingest_upstream_row(self, row: dict[str, Any], *, run_id: str | None = None) -> dict[str, Any]:
        source_id = str(row.get("source_id") or "").strip()
        artifact_id = str(row.get("artifact_id") or "").strip()
        if not source_id or not artifact_id:
            raise ValueError("upstream row requires source_id and artifact_id")
        now = _now()
        status = self._physical_state(row)
        local_path = str(row.get("local_path") or "").strip() or None
        incoming_sha = str(row.get("sha256") or "").strip() or None
        size = row.get("size_bytes")
        existing = self.get(source_id, artifact_id)
        effective_sha = incoming_sha or (str(existing.get("sha256") or "").strip() or None)
        effective_path = local_path or (str(existing.get("local_path") or "").strip() or None)
        preserve_verification = bool(
            existing
            and str(existing.get("verification_status") or "") == "VERIFIED"
            and str(existing.get("verified_sha256") or "") == str(effective_sha or "")
            and str(existing.get("local_path") or "") == str(effective_path or "")
            and status in PHYSICAL_STATUSES
        )
        verification_status = "VERIFIED" if preserve_verification else "UNVERIFIED"
        verified_at = existing.get("verified_at") if preserve_verification else None
        verified_sha256 = existing.get("verified_sha256") if preserve_verification else None

        self.db.execute(
            """
            INSERT INTO artifacts(
                source_id,artifact_id,upstream_id,upstream_url,artifact_type,status,
                upstream_signature,etag,last_modified,sha256,size_bytes,local_path,
                first_seen_at,last_checked_at,downloaded_at,verified_at,verification_status,
                verified_sha256,http_status,fetch_method,error,last_run_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id,artifact_id) DO UPDATE SET
                upstream_id=COALESCE(excluded.upstream_id,artifacts.upstream_id),
                upstream_url=COALESCE(excluded.upstream_url,artifacts.upstream_url),
                artifact_type=COALESCE(excluded.artifact_type,artifacts.artifact_type),
                status=excluded.status,
                upstream_signature=COALESCE(excluded.upstream_signature,artifacts.upstream_signature),
                etag=COALESCE(excluded.etag,artifacts.etag),
                last_modified=COALESCE(excluded.last_modified,artifacts.last_modified),
                sha256=COALESCE(excluded.sha256,artifacts.sha256),
                size_bytes=COALESCE(excluded.size_bytes,artifacts.size_bytes),
                local_path=COALESCE(excluded.local_path,artifacts.local_path),
                last_checked_at=COALESCE(excluded.last_checked_at,artifacts.last_checked_at),
                downloaded_at=COALESCE(excluded.downloaded_at,artifacts.downloaded_at),
                verified_at=excluded.verified_at,
                verification_status=excluded.verification_status,
                verified_sha256=excluded.verified_sha256,
                http_status=excluded.http_status,
                fetch_method=COALESCE(excluded.fetch_method,artifacts.fetch_method),
                error=excluded.error,
                last_run_id=COALESCE(excluded.last_run_id,artifacts.last_run_id)
            """,
            (
                source_id,
                artifact_id,
                str(row.get("upstream_id") or artifact_id) or None,
                str(row.get("url") or "").strip() or None,
                str(row.get("artifact_type") or artifact_id.split(":", 1)[0] or "artifact"),
                status,
                str(row.get("signature") or "").strip() or None,
                str(row.get("etag") or "").strip() or None,
                str(row.get("last_modified") or "").strip() or None,
                incoming_sha,
                int(size) if size not in (None, "") else None,
                local_path,
                existing.get("first_seen_at") if existing else now,
                str(row.get("last_checked") or now),
                str(row.get("last_downloaded") or "").strip() or None,
                verified_at,
                verification_status,
                verified_sha256,
                int(row["http_status"]) if row.get("http_status") is not None else None,
                str(row.get("method") or "").strip() or None,
                str(row.get("error") or "").strip()[-2000:] or None,
                run_id,
            ),
        )
        if run_id:
            self.db.execute(
                """
                INSERT OR REPLACE INTO run_artifacts(
                    run_id,source_id,artifact_id,status,size_bytes,local_path,observed_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (run_id, source_id, artifact_id, status, int(size or 0), local_path, now),
            )
        self.db.commit()
        return self.get(source_id, artifact_id)

    def ingest_upstream_rows(self, rows: Iterable[dict[str, Any]], *, run_id: str | None = None) -> int:
        count = 0
        for row in rows:
            self.ingest_upstream_row(dict(row), run_id=run_id)
            count += 1
        return count

    def get(self, source_id: str, artifact_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM artifacts WHERE source_id=? AND artifact_id=?",
            (source_id, artifact_id),
        ).fetchone()
        return dict(row) if row else {}

    def verify(self, *, source_id: str | None = None, limit: int | None = None) -> dict[str, Any]:
        query = "SELECT * FROM artifacts"
        params: list[Any] = []
        if source_id:
            query += " WHERE source_id=?"
            params.append(source_id)
        query += " ORDER BY source_id, artifact_id"
        if limit is not None and int(limit) > 0:
            query += " LIMIT ?"
            params.append(int(limit))
        rows = self.db.execute(query, params).fetchall()
        checked = 0
        verified = 0
        missing = 0
        corrupted = 0
        skipped = 0
        for raw in rows:
            row = dict(raw)
            status = str(row.get("status") or "DISCOVERED")
            if status in TERMINAL_NON_PHYSICAL or status == "DISCOVERED":
                skipped += 1
                continue
            checked += 1
            path_value = str(row.get("local_path") or "").strip()
            if not path_value or not Path(path_value).is_file():
                self._set_verification(row, "LOCAL_MISSING", error="local artifact is missing")
                missing += 1
                continue
            path = Path(path_value)
            actual_size = path.stat().st_size
            expected_size = row.get("size_bytes")
            if expected_size is not None and int(expected_size) != actual_size:
                self._set_verification(
                    row,
                    "CORRUPTED",
                    error=f"size mismatch expected={int(expected_size)} actual={actual_size}",
                )
                corrupted += 1
                continue
            actual_hash = _sha256(path)
            expected_hash = str(row.get("sha256") or "").strip()
            if expected_hash and expected_hash.casefold() != actual_hash.casefold():
                self._set_verification(
                    row,
                    "CORRUPTED",
                    error=f"sha256 mismatch expected={expected_hash} actual={actual_hash}",
                )
                corrupted += 1
                continue
            restored_status = "FETCHED" if status in {"LOCAL_MISSING", "CORRUPTED"} else status
            self.db.execute(
                """
                UPDATE artifacts SET status=?, sha256=?, size_bytes=?, verified_at=?,
                    verification_status='VERIFIED', verified_sha256=?, last_checked_at=?, error=NULL
                WHERE source_id=? AND artifact_id=?
                """,
                (
                    restored_status,
                    actual_hash,
                    actual_size,
                    _now(),
                    actual_hash,
                    _now(),
                    row["source_id"],
                    row["artifact_id"],
                ),
            )
            self.db.commit()
            verified += 1
        return {
            "database": str(self.path),
            "source_id": source_id,
            "checked": checked,
            "verified": verified,
            "local_missing": missing,
            "corrupted": corrupted,
            "skipped": skipped,
            "audit": self.audit(source_id=source_id),
        }

    def _set_verification(self, row: dict[str, Any], status: str, *, error: str) -> None:
        self.db.execute(
            """
            UPDATE artifacts SET status=?, verification_status='FAILED', verified_sha256=NULL,
                verified_at=NULL, last_checked_at=?, error=?
            WHERE source_id=? AND artifact_id=?
            """,
            (status, _now(), error[-2000:], row["source_id"], row["artifact_id"]),
        )
        self.db.commit()

    def mark_removed(self, source_id: str, artifact_id: str, *, reason: str | None = None) -> None:
        self.db.execute(
            """
            UPDATE artifacts SET status='REMOVED', last_checked_at=?, error=?
            WHERE source_id=? AND artifact_id=?
            """,
            (_now(), str(reason)[-2000:] if reason else None, source_id, artifact_id),
        )
        self.db.commit()

    def repair_plan(self, *, source_id: str | None = None) -> dict[str, Any]:
        placeholders = ",".join("?" for _ in REPAIRABLE_STATUSES)
        query = f"SELECT * FROM artifacts WHERE status IN ({placeholders})"
        params: list[Any] = sorted(REPAIRABLE_STATUSES)
        if source_id:
            query += " AND source_id=?"
            params.append(source_id)
        query += " ORDER BY source_id, artifact_id"
        rows = [dict(row) for row in self.db.execute(query, params).fetchall()]
        source_ids = sorted({str(row["source_id"]) for row in rows})
        return {
            "database": str(self.path),
            "repairable_artifacts": len(rows),
            "source_ids": source_ids,
            "artifacts": [
                {
                    "source_id": row["source_id"],
                    "artifact_id": row["artifact_id"],
                    "status": row["status"],
                    "local_path": row.get("local_path"),
                    "error": row.get("error"),
                }
                for row in rows
            ],
        }

    def audit(self, *, source_id: str | None = None) -> dict[str, Any]:
        where = " WHERE source_id=?" if source_id else ""
        params = (source_id,) if source_id else ()
        status_rows = self.db.execute(
            f"SELECT status, COUNT(*) AS n FROM artifacts{where} GROUP BY status ORDER BY status",
            params,
        ).fetchall()
        verification_rows = self.db.execute(
            f"SELECT verification_status, COUNT(*) AS n FROM artifacts{where} GROUP BY verification_status ORDER BY verification_status",
            params,
        ).fetchall()
        summary_row = self.db.execute(
            f"""
            SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS bytes,
                   SUM(CASE WHEN verification_status='VERIFIED' THEN 1 ELSE 0 END) AS verified
            FROM artifacts{where}
            """,
            params,
        ).fetchone()
        missing_paths = 0
        claimed_physical = 0
        rows = self.db.execute(
            f"SELECT status,local_path FROM artifacts{where}", params
        ).fetchall()
        for row in rows:
            if str(row["status"]) in PHYSICAL_STATUSES:
                claimed_physical += 1
                value = str(row["local_path"] or "").strip()
                if not value or not Path(value).is_file():
                    missing_paths += 1
        run_where = " WHERE source_id=?" if source_id else ""
        recent_runs = [
            dict(row)
            for row in self.db.execute(
                f"SELECT * FROM runs{run_where} ORDER BY started_at DESC LIMIT 20", params
            ).fetchall()
        ]
        return {
            "database": str(self.path),
            "schema_version": self.schema_version,
            "source_id": source_id,
            "artifacts": int(summary_row["n"] or 0),
            "bytes_recorded": int(summary_row["bytes"] or 0),
            "by_status": {str(row["status"]): int(row["n"]) for row in status_rows},
            "verification_by_status": {
                str(row["verification_status"]): int(row["n"]) for row in verification_rows
            },
            "verified_artifacts": int(summary_row["verified"] or 0),
            "claimed_physical": claimed_physical,
            "missing_physical_paths": missing_paths,
            "recent_runs": recent_runs,
        }
