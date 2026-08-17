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

    When a logical source changes roots, the previous physical data directory is moved
    under ``programming_docs/_superseded`` before the new target is fetched. Nothing is
    deleted; the old canary evidence remains auditable but is no longer part of the
    live documentation tree.
    """

    def _init_schema(self) -> None:
        super()._init_schema()
        columns = {
            str(row["name"])
            for row in self.db.execute("PRAGMA table_info(documentation_fetch_state)").fetchall()
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
                migrated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_docs_target_migrations_source
                ON documentation_target_migrations(registry,name,migrated_at DESC);
            """
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
            previous.get("physical_source_id")
            or previous.get("source_id")
            or target["source_id"]
        )
        root = self._find_live_root(old_physical)
        if root is None:
            return None
        logical = safe_name(str(target["source_id"]))
        old_hash = _target_generation(str(previous.get("target_url") or "unknown"))
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

    def _prepare_target_transition(self, target: dict[str, Any]) -> dict[str, Any] | None:
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

        new_physical = self._physical_source_id(target)
        quarantine = self._quarantine_previous_generation(target, previous)
        with self.db:
            self.db.execute(
                """
                INSERT INTO documentation_target_migrations(
                    registry,name,logical_source_id,old_target_url,new_target_url,
                    old_physical_source_id,new_physical_source_id,quarantine_path,migrated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    target["registry"],
                    target["name"],
                    target["source_id"],
                    old_url,
                    new_url,
                    previous.get("physical_source_id") or previous.get("source_id"),
                    new_physical,
                    quarantine,
                    datetime.now(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                ),
            )
        return {
            "old_target_url": old_url,
            "new_target_url": new_url,
            "old_physical_source_id": previous.get("physical_source_id")
            or previous.get("source_id"),
            "new_physical_source_id": new_physical,
            "quarantine_path": quarantine,
        }

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
            outcome["target_migration"] = migration
        return outcome

    def audit(self, *, top: int = 50) -> dict[str, Any]:
        payload = super().audit(top=top)
        migration_count = self.db.execute(
            "SELECT COUNT(*) AS n FROM documentation_target_migrations"
        ).fetchone()
        migrations = [
            dict(row)
            for row in self.db.execute(
                """
                SELECT registry,name,logical_source_id,old_target_url,new_target_url,
                       old_physical_source_id,new_physical_source_id,quarantine_path,migrated_at
                FROM documentation_target_migrations
                ORDER BY migrated_at DESC,id DESC LIMIT ?
                """,
                (max(1, int(top)),),
            ).fetchall()
        ]
        payload["engine"] = "dynamic-documentation-fetcher-v2"
        payload["target_migrations"] = int(
            migration_count["n"] if migration_count else 0
        )
        payload["recent_target_migrations"] = migrations
        return payload
