from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class HarvestCandidate:
    registry: str
    name: str
    source: str
    priority: int = 0
    repository_hint: str | None = None
    type_hint: str | None = None
    abandoned: str | None = None
    requeue: bool = False


class TechnologyHarvestQueue:
    """SQLite-backed global candidate queue.

    The verified technology catalog stays compact JSON. This database can safely hold
    hundreds of thousands or millions of package names without rewriting one giant file.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE IF NOT EXISTS candidates (
                registry TEXT NOT NULL,
                name TEXT NOT NULL,
                source TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                repository_hint TEXT,
                type_hint TEXT,
                abandoned TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_error TEXT,
                PRIMARY KEY (registry, name)
            );
            CREATE INDEX IF NOT EXISTS idx_candidates_status_priority
                ON candidates(status, priority DESC, last_seen_at DESC);
            CREATE TABLE IF NOT EXISTS cursors (
                source TEXT PRIMARY KEY,
                cursor TEXT,
                etag TEXT,
                last_modified TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                discovered INTEGER NOT NULL DEFAULT 0,
                inserted INTEGER NOT NULL DEFAULT 0,
                updated INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def upsert_many(self, candidates: Iterable[HarvestCandidate]) -> tuple[int, int]:
        inserted = 0
        updated = 0
        now = _now()
        for candidate in candidates:
            previous = self.db.execute(
                "SELECT status FROM candidates WHERE registry=? AND name=?",
                (candidate.registry, candidate.name),
            ).fetchone()
            self.db.execute(
                """
                INSERT INTO candidates(
                    registry,name,source,priority,repository_hint,type_hint,abandoned,status,
                    attempts,first_seen_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,?,'PENDING',0,?,?)
                ON CONFLICT(registry,name) DO UPDATE SET
                    source=excluded.source,
                    priority=MAX(candidates.priority, excluded.priority),
                    repository_hint=COALESCE(excluded.repository_hint,candidates.repository_hint),
                    type_hint=COALESCE(excluded.type_hint,candidates.type_hint),
                    abandoned=COALESCE(excluded.abandoned,candidates.abandoned),
                    status=CASE WHEN ? THEN 'PENDING' ELSE candidates.status END,
                    last_error=CASE WHEN ? THEN NULL ELSE candidates.last_error END,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    candidate.registry,
                    candidate.name,
                    candidate.source,
                    int(candidate.priority),
                    candidate.repository_hint,
                    candidate.type_hint,
                    candidate.abandoned,
                    now,
                    now,
                    bool(candidate.requeue),
                    bool(candidate.requeue),
                ),
            )
            if previous:
                updated += 1
            else:
                inserted += 1
        self.db.commit()
        return inserted, updated

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT * FROM candidates
            WHERE status IN ('PENDING','RETRY')
            ORDER BY priority DESC, last_seen_at DESC, name ASC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_qualified(self, registry: str, name: str) -> None:
        self.db.execute(
            "UPDATE candidates SET status='QUALIFIED', attempts=attempts+1, last_error=NULL WHERE registry=? AND name=?",
            (registry, name),
        )
        self.db.commit()

    def mark_error(self, registry: str, name: str, error: str) -> None:
        self.db.execute(
            "UPDATE candidates SET status='RETRY', attempts=attempts+1, last_error=? WHERE registry=? AND name=?",
            (str(error)[:1000], registry, name),
        )
        self.db.commit()

    def cursor(self, source: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM cursors WHERE source=?", (source,)).fetchone()
        return dict(row) if row else {}

    def set_cursor(self, source: str, *, cursor: str | None = None, etag: str | None = None, last_modified: str | None = None) -> None:
        self.db.execute(
            """
            INSERT INTO cursors(source,cursor,etag,last_modified,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(source) DO UPDATE SET
                cursor=excluded.cursor,
                etag=COALESCE(excluded.etag,cursors.etag),
                last_modified=COALESCE(excluded.last_modified,cursors.last_modified),
                updated_at=excluded.updated_at
            """,
            (source, cursor, etag, last_modified, _now()),
        )
        self.db.commit()

    def reset_cursor(self, source: str) -> None:
        self.db.execute("DELETE FROM cursors WHERE source=?", (source,))
        self.db.commit()

    def audit(self) -> dict[str, Any]:
        status_rows = self.db.execute("SELECT status, COUNT(*) AS n FROM candidates GROUP BY status").fetchall()
        registry_rows = self.db.execute("SELECT registry, COUNT(*) AS n FROM candidates GROUP BY registry ORDER BY n DESC").fetchall()
        return {
            "database": str(self.path),
            "candidates": sum(int(row["n"]) for row in status_rows),
            "by_status": {row["status"]: int(row["n"]) for row in status_rows},
            "by_registry": {row["registry"]: int(row["n"]) for row in registry_rows},
            "cursors": [dict(row) for row in self.db.execute("SELECT * FROM cursors ORDER BY source").fetchall()],
        }


class RegistryHarvester:
    def __init__(self, *, queue: TechnologyHarvestQueue, user_agent: str, session: requests.Session | None = None):
        self.queue = queue
        self.user_agent = user_agent
        self.session = session or requests.Session()

    def _get(self, url: str, *, headers: dict[str, str] | None = None, params: Any = None) -> requests.Response:
        merged = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if headers:
            merged.update(headers)
        response = self.session.get(url, headers=merged, params=params, timeout=120)
        response.raise_for_status()
        return response

    def harvest_packagist_popular(self, *, limit: int = 500) -> dict[str, Any]:
        candidates: list[HarvestCandidate] = []
        page = 1
        target = max(1, int(limit))
        while len(candidates) < target:
            response = self._get("https://packagist.org/explore/popular.json", params={"per_page": min(100, target - len(candidates)), "page": page})
            payload = response.json()
            rows = payload.get("packages") or []
            if not rows:
                break
            for row in rows:
                if not isinstance(row, dict) or not row.get("name"):
                    continue
                downloads = int(row.get("downloads") or 0)
                favers = int(row.get("favers") or 0)
                priority = min(100, 40 + min(40, downloads // 100000) + min(20, favers // 100))
                candidates.append(HarvestCandidate("packagist.org", str(row["name"]), "packagist-popular", priority))
                if len(candidates) >= target:
                    break
            if not payload.get("next"):
                break
            page += 1
        inserted, updated = self.queue.upsert_many(candidates)
        return {"source": "packagist-popular", "discovered": len(candidates), "inserted": inserted, "updated": updated}

    def harvest_packagist_all(self, *, limit: int = 0) -> dict[str, Any]:
        response = self._get(
            "https://packagist.org/packages/list.json",
            params=[("fields[]", "repository"), ("fields[]", "type"), ("fields[]", "abandoned")],
        )
        payload = response.json()
        package_map = payload.get("package") or {}
        names = list(package_map.keys())
        if limit > 0:
            names = names[: int(limit)]
        candidates = []
        for name in names:
            meta = package_map.get(name) or {}
            candidates.append(HarvestCandidate(
                "packagist.org",
                str(name),
                "packagist-all",
                20,
                repository_hint=str(meta.get("repository")) if meta.get("repository") else None,
                type_hint=str(meta.get("type")) if meta.get("type") else None,
                abandoned=str(meta.get("abandoned")) if meta.get("abandoned") not in (None, False) else None,
            ))
        inserted, updated = self.queue.upsert_many(candidates)
        return {"source": "packagist-all", "discovered": len(candidates), "inserted": inserted, "updated": updated}

    def harvest_packagist_changes(self, *, limit: int = 1000) -> dict[str, Any]:
        source = "packagist-changes"
        state = self.queue.cursor(source)
        params = {"since": state.get("cursor")} if state.get("cursor") else None
        response = self._get("https://packagist.org/metadata/changes.json", params=params)
        payload = response.json()
        timestamp = payload.get("timestamp")
        actions = payload.get("actions") or []
        candidates: list[HarvestCandidate] = []
        resync = False
        for action in actions[: max(1, int(limit))]:
            if not isinstance(action, dict):
                continue
            if action.get("type") == "resync":
                resync = True
                continue
            if action.get("type") == "delete":
                continue
            name = str(action.get("package") or "").replace("~dev", "")
            if name:
                candidates.append(HarvestCandidate("packagist.org", name, source, 60, requeue=True))
        inserted, updated = self.queue.upsert_many(candidates)
        if timestamp is not None:
            self.queue.set_cursor(source, cursor=str(timestamp))
        return {"source": source, "discovered": len(candidates), "inserted": inserted, "updated": updated, "resync_required": resync}

    def harvest_rubygems_activity(self) -> dict[str, Any]:
        candidates: dict[str, HarvestCandidate] = {}
        for endpoint, priority in (("latest", 45), ("just_updated", 55)):
            response = self._get(f"https://rubygems.org/api/v1/activity/{endpoint}.json")
            rows = response.json()
            for row in rows if isinstance(rows, list) else []:
                name = str((row or {}).get("name") or "").strip()
                if name:
                    candidates[name] = HarvestCandidate(
                        "rubygems.org", name, f"rubygems-{endpoint}", priority,
                        requeue=endpoint == "just_updated",
                    )
        inserted, updated = self.queue.upsert_many(candidates.values())
        return {"source": "rubygems-activity", "discovered": len(candidates), "inserted": inserted, "updated": updated}

    def harvest_pubdev(self, *, limit: int = 500, full: bool = False) -> dict[str, Any]:
        source = "pubdev-package-names"
        state = self.queue.cursor(source)
        if full:
            self.queue.reset_cursor(source)
            state = {}
        if state.get("cursor") == "__COMPLETE__":
            return {"source": source, "complete": True, "discovered": 0, "inserted": 0, "updated": 0}
        next_url = state.get("cursor") or "https://pub.dev/api/package-names"
        candidates: list[HarvestCandidate] = []
        target = max(1, int(limit))
        while next_url and len(candidates) < target:
            response = self._get(str(next_url), headers={"Accept-Encoding": "gzip"})
            payload = response.json()
            for name in payload.get("packages") or []:
                candidates.append(HarvestCandidate("pub.dev", str(name), source, 30))
                if len(candidates) >= target:
                    break
            next_url = payload.get("nextUrl")
        inserted, updated = self.queue.upsert_many(candidates)
        self.queue.set_cursor(source, cursor=str(next_url) if next_url else "__COMPLETE__")
        return {
            "source": source,
            "discovered": len(candidates),
            "inserted": inserted,
            "updated": updated,
            "complete": next_url is None,
            "next_url": next_url,
        }

    def harvest_pypi(self, *, limit: int = 1000, full: bool = False) -> dict[str, Any]:
        if not full:
            raise ValueError("PyPI full index enumeration is intentionally explicit; pass full=True")
        source = "pypi-simple"
        state = self.queue.cursor(source)
        headers = {"Accept": "application/vnd.pypi.simple.v1+json"}
        if state.get("etag"):
            headers["If-None-Match"] = str(state["etag"])
        response = self.session.get("https://pypi.org/simple/", headers={"User-Agent": self.user_agent, **headers}, timeout=180)
        if response.status_code == 304:
            return {"source": source, "not_modified": True, "discovered": 0, "inserted": 0, "updated": 0}
        response.raise_for_status()
        payload = response.json()
        projects = payload.get("projects") or []
        if limit > 0:
            projects = projects[: int(limit)]
        candidates = [HarvestCandidate("pypi.org", str(row.get("name")), source, 10) for row in projects if isinstance(row, dict) and row.get("name")]
        inserted, updated = self.queue.upsert_many(candidates)
        serial = (payload.get("meta") or {}).get("_last-serial") or response.headers.get("X-PyPI-Last-Serial")
        self.queue.set_cursor(source, cursor=str(serial) if serial is not None else None, etag=response.headers.get("ETag"), last_modified=response.headers.get("Last-Modified"))
        return {"source": source, "discovered": len(candidates), "inserted": inserted, "updated": updated, "serial": serial}

    def harvest(self, registry: str, *, limit: int = 500, full: bool = False) -> dict[str, Any]:
        key = registry.strip().casefold()
        if key in {"packagist", "composer"}:
            return self.harvest_packagist_all(limit=limit) if full else self.harvest_packagist_popular(limit=limit)
        if key in {"packagist-changes", "composer-changes"}:
            return self.harvest_packagist_changes(limit=limit)
        if key in {"rubygems", "gem"}:
            return self.harvest_rubygems_activity()
        if key in {"pub", "pubdev", "dart"}:
            return self.harvest_pubdev(limit=limit, full=full)
        if key in {"pypi", "python"}:
            return self.harvest_pypi(limit=limit, full=full)
        raise ValueError(f"no official bulk harvester yet for {registry!r}")


def qualify_pending(*, queue: TechnologyHarvestQueue, catalog_engine: Any, limit: int = 50) -> dict[str, Any]:
    selected = queue.pending(limit=limit)
    success = 0
    failed: list[dict[str, str]] = []
    for candidate in selected:
        registry = str(candidate["registry"])
        name = str(candidate["name"])
        try:
            catalog_engine.discover_package(registry, name)
            queue.mark_qualified(registry, name)
            success += 1
        except Exception as exc:  # one package cannot stop qualification
            queue.mark_error(registry, name, str(exc))
            failed.append({"registry": registry, "name": name, "error": str(exc)[:500]})
    return {"selected": len(selected), "success": success, "failed": len(failed), "failures": failed[:50]}
