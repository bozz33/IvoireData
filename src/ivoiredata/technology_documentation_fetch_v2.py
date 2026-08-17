from __future__ import annotations

import hashlib
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .delivery import safe_name
from .models import SourceSpec
from .technology_documentation import canonical_documentation_url
from .technology_documentation_fetch import DynamicDocumentationFetcher as _BaseFetcher


def _now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _target_generation(url: str) -> str:
    canonical = canonical_documentation_url(url) or str(url or "")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


class DynamicDocumentationFetcher(_BaseFetcher):
    """Dynamic docs fetcher v2 with target-generation isolation.

    A package keeps one stable logical source id in the technology SQLite model, but
    each *documentation root* gets a physical source id derived from the canonical
    target URL. Version changes on the same docs root therefore reuse ETag/SHA/dlt
    state, while a real root change (for example Maven Central -> official project
    docs) cannot mix old pages/chunks with the new corpus.

    Root migration is two-phase. The previous corpus remains live while the replacement
    is fetched under its independent physical source id. Only after the replacement is
    SUCCESS (or safely aliased to existing coverage) is the previous live directory
    moved under ``programming_docs/_superseded``. A failed replacement therefore never
    destroys or hides the last usable generation.
    """

    _COVERED_STATUSES = {
        "SUCCESS",
        "ALIASED_STATIC_SOURCE",
        "ALIASED_DYNAMIC_SOURCE",
    }

    def _init_schema(self) -> None:
        super()._init_schema()
        columns = {
            str(row["name"])
            for row in self.db.execute(
                "PRAGMA table_info(documentation_fetch_state)"
            ).fetchall()
        }
        if "physical_source_id" not in columns:
            self.db.execute(
                "ALTER TABLE documentation_fetch_state ADD COLUMN physical_source_id TEXT"
            )
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS documentation_target_migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                registry TEXT NOT NULL,
                name TEXT NOT NULL,
                logical_source_id TEXT NOT NULL,
                old_target_url TEXT,
                new_target_url TEXT NOT NULL,
                old_physical_source_id TEXT,
                new_physical_source_id TEXT NOT NULL,
                quarantine_path TEXT,
                migration_status TEXT NOT NULL DEFAULT 'PENDING',
                migrated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_docs_target_migrations_source
                ON documentation_target_migrations(registry,name,migrated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_docs_target_migrations_pending
                ON documentation_target_migrations(registry,name,migration_status,new_target_url);
            """
        )
        migration_columns = {
            str(row["name"])
            for row in self.db.execute(
                "PRAGMA table_info(documentation_target_migrations)"
            ).fetchall()
        }
        if "migration_status" not in migration_columns:
            self.db.execute(
                "ALTER TABLE documentation_target_migrations "
                "ADD COLUMN migration_status TEXT NOT NULL DEFAULT 'COMPLETED'"
            )
        if "completed_at" not in migration_columns:
            self.db.execute(
                "ALTER TABLE documentation_target_migrations ADD COLUMN completed_at TEXT"
            )
        self.db.commit()

    @staticmethod
    def _physical_source_id(target: dict[str, Any]) -> str:
        logical = str(target["source_id"])
        generation = _target_generation(str(target["target_url"]))
        return f"{logical}-g{generation}"

    def _spec(self, target: dict[str, Any]) -> SourceSpec:
        base = super()._spec(target)
        physical = self._physical_source_id(target)
        options = dict(base.options)
        options.update(
            {
                "logical_source_id": str(target["source_id"]),
                "documentation_target_generation": _target_generation(
                    str(target["target_url"])
                ),
            }
        )
        return replace(base, source_id=physical, options=options)

    def _find_live_root(self, physical_source_id: str) -> Path | None:
        docs_root = self.settings.data_dir / "programming_docs"
        if not docs_root.exists():
            return None
        safe = safe_name(physical_source_id)
        for language_dir in docs_root.iterdir():
            if not language_dir.is_dir() or language_dir.name == "_superseded":
                continue
            candidate = language_dir / safe
            if candidate.exists():
                return candidate
        return None

    def _quarantine_previous_generation(
        self,
        target: dict[str, Any],
        previous: dict[str, Any],
    ) -> str | None:
        old_physical = str(
            previous.get("old_physical_source_id")
            or previous.get("physical_source_id")
            or previous.get("source_id")
            or target["source_id"]
        )
        root = self._find_live_root(old_physical)
        if root is None:
            return None
        logical = safe_name(str(target["source_id"]))
        old_hash = _target_generation(
            str(previous.get("old_target_url") or previous.get("target_url") or "unknown")
        )
        destination = (
            self.settings.data_dir
            / "programming_docs"
            / "_superseded"
            / logical
            / f"{_now_compact()}-{safe_name(old_physical)}-{old_hash}"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        suffix = 0
        final = destination
        while final.exists():
            suffix += 1
            final = destination.with_name(destination.name + f"-{suffix}")
        shutil.move(str(root), str(final))
        return str(final)

    def _pending_transition(self, target: dict[str, Any]) -> dict[str, Any] | None:
        new_url = canonical_documentation_url(target.get("target_url"))
        if not new_url:
            return None
        row = self.db.execute(
            """
            SELECT * FROM documentation_target_migrations
            WHERE registry=? AND name=? AND migration_status='PENDING'
              AND new_target_url=?
            ORDER BY id DESC LIMIT 1
            """,
            (target["registry"], target["name"], new_url),
        ).fetchone()
        return dict(row) if row is not None else None

    def _prepare_target_transition(
        self,
        target: dict[str, Any],
    ) -> dict[str, Any] | None:
        pending = self._pending_transition(target)
        if pending is not None:
            return pending

        row = self.db.execute(
            """
            SELECT source_id,target_url,fetch_status,physical_source_id
            FROM documentation_fetch_state
            WHERE registry=? AND name=?
            """,
            (target["registry"], target["name"]),
        ).fetchone()
        if row is None:
            return None
        previous = dict(row)
        old_url = canonical_documentation_url(previous.get("target_url"))
        new_url = canonical_documentation_url(target.get("target_url"))
        if not old_url or not new_url or old_url == new_url:
            return None

        old_physical = previous.get("physical_source_id") or previous.get("source_id")
        new_physical = self._physical_source_id(target)
        now = _now_iso()
        with self.db:
            cursor = self.db.execute(
                """
                INSERT INTO documentation_target_migrations(
                    registry,name,logical_source_id,old_target_url,new_target_url,
                    old_physical_source_id,new_physical_source_id,quarantine_path,
                    migration_status,migrated_at,completed_at
                ) VALUES(?,?,?,?,?,?,?,NULL,'PENDING',?,NULL)
                """,
                (
                    target["registry"],
                    target["name"],
                    target["source_id"],
                    old_url,
                    new_url,
                    old_physical,
                    new_physical,
                    now,
                ),
            )
            migration_id = int(cursor.lastrowid)
        return {
            "id": migration_id,
            "registry": target["registry"],
            "name": target["name"],
            "logical_source_id": target["source_id"],
            "old_target_url": old_url,
            "new_target_url": new_url,
            "old_physical_source_id": old_physical,
            "new_physical_source_id": new_physical,
            "quarantine_path": None,
            "migration_status": "PENDING",
            "migrated_at": now,
            "completed_at": None,
        }

    def _finalize_target_transition(
        self,
        target: dict[str, Any],
        migration: dict[str, Any],
    ) -> dict[str, Any]:
        quarantine = self._quarantine_previous_generation(target, migration)
        completed = _now_iso()
        with self.db:
            self.db.execute(
                """
                UPDATE documentation_target_migrations
                SET quarantine_path=?,migration_status='COMPLETED',completed_at=?
                WHERE id=?
                """,
                (quarantine, completed, int(migration["id"])),
            )
        result = dict(migration)
        result["quarantine_path"] = quarantine
        result["migration_status"] = "COMPLETED"
        result["completed_at"] = completed
        return result

    def _save(self, target: dict[str, Any], **kwargs) -> dict[str, Any]:
        outcome = super()._save(target, **kwargs)
        physical = self._physical_source_id(target)
        with self.db:
            self.db.execute(
                """
                UPDATE documentation_fetch_state
                SET physical_source_id=?
                WHERE registry=? AND name=?
                """,
                (physical, target["registry"], target["name"]),
            )
        outcome["logical_source_id"] = str(target["source_id"])
        outcome["physical_source_id"] = physical
        return outcome

    def process(self, target: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        target = self._canonical_target(target)
        migration = self._prepare_target_transition(target)
        outcome = super().process(target, force=force)
        if migration is not None:
            if str(outcome.get("status") or "") in self._COVERED_STATUSES:
                migration = self._finalize_target_transition(target, migration)
            outcome["target_migration"] = migration
        return outcome

    def audit(self, *, top: int = 50) -> dict[str, Any]:
        payload = super().audit(top=top)
        completed_count = self.db.execute(
            """
            SELECT COUNT(*) AS n FROM documentation_target_migrations
            WHERE migration_status='COMPLETED'
            """
        ).fetchone()
        pending_count = self.db.execute(
            """
            SELECT COUNT(*) AS n FROM documentation_target_migrations
            WHERE migration_status='PENDING'
            """
        ).fetchone()
        migrations = [
            dict(row)
            for row in self.db.execute(
                """
                SELECT registry,name,logical_source_id,old_target_url,new_target_url,
                       old_physical_source_id,new_physical_source_id,quarantine_path,
                       migration_status,migrated_at,completed_at
                FROM documentation_target_migrations
                ORDER BY id DESC LIMIT ?
                """,
                (max(1, int(top)),),
            ).fetchall()
        ]
        payload["engine"] = "dynamic-documentation-fetcher-v2"
        payload["target_migrations"] = int(
            completed_count["n"] if completed_count else 0
        )
        payload["pending_target_migrations"] = int(
            pending_count["n"] if pending_count else 0
        )
        payload["recent_target_migrations"] = migrations
        return payload
