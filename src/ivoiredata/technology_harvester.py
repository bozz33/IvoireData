from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests


NPM_REPLICATION_ROOTS = (
    "https://replicate.npmjs.com/",
    "https://replicate.npmjs.com/registry/",
)
NPM_ALL_DOCS_URL = "https://replicate.npmjs.com/registry/_all_docs"
NPM_CHANGES_URL = "https://replicate.npmjs.com/registry/_changes"
NPM_BOOTSTRAP_SOURCE = "npm-all-docs"
NPM_CHANGES_SOURCE = "npm-changes"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _cursor_json(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _dump_cursor(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _npm_package_name(value: Any) -> str | None:
    name = str(value or "").strip()
    if not name or name.startswith("_design/") or name.startswith("_local/"):
        return None
    return name


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
    seen_token: str | None = None


class TechnologyHarvestQueue:
    """SQLite-backed global candidate queue.

    The verified technology catalog stays compact JSON. This database can safely hold
    hundreds of thousands or millions of package names without rewriting one giant file.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=60)
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            PRAGMA busy_timeout=60000;
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
            """
        )
        columns = {
            str(row["name"])
            for row in self.db.execute("PRAGMA table_info(candidates)").fetchall()
        }
        if "last_seen_token" not in columns:
            self.db.execute("ALTER TABLE candidates ADD COLUMN last_seen_token TEXT")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def _upsert_one(self, candidate: HarvestCandidate, *, now: str) -> bool:
        previous = self.db.execute(
            "SELECT status FROM candidates WHERE registry=? AND name=?",
            (candidate.registry, candidate.name),
        ).fetchone()
        self.db.execute(
            """
            INSERT INTO candidates(
                registry,name,source,priority,repository_hint,type_hint,abandoned,status,
                attempts,first_seen_at,last_seen_at,last_seen_token
            ) VALUES(?,?,?,?,?,?,?,'PENDING',0,?,?,?)
            ON CONFLICT(registry,name) DO UPDATE SET
                source=excluded.source,
                priority=MAX(candidates.priority, excluded.priority),
                repository_hint=COALESCE(excluded.repository_hint,candidates.repository_hint),
                type_hint=COALESCE(excluded.type_hint,candidates.type_hint),
                abandoned=COALESCE(excluded.abandoned,candidates.abandoned),
                status=CASE WHEN ? THEN 'PENDING' ELSE candidates.status END,
                last_error=CASE WHEN ? THEN NULL ELSE candidates.last_error END,
                last_seen_at=excluded.last_seen_at,
                last_seen_token=COALESCE(excluded.last_seen_token,candidates.last_seen_token)
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
                candidate.seen_token,
                bool(candidate.requeue),
                bool(candidate.requeue),
            ),
        )
        return previous is None

    def _set_cursor_no_commit(
        self,
        source: str,
        *,
        cursor: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
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

    def _mark_deleted_no_commit(self, registry: str, name: str, *, source: str, now: str) -> bool:
        previous = self.db.execute(
            "SELECT status FROM candidates WHERE registry=? AND name=?",
            (registry, name),
        ).fetchone()
        self.db.execute(
            """
            INSERT INTO candidates(
                registry,name,source,priority,status,attempts,first_seen_at,last_seen_at,last_error
            ) VALUES(?,?,?,0,'DELETED',0,?,?,NULL)
            ON CONFLICT(registry,name) DO UPDATE SET
                source=excluded.source,
                status='DELETED',
                last_seen_at=excluded.last_seen_at,
                last_error=NULL
            """,
            (registry, name, source, now, now),
        )
        return previous is None

    def upsert_many(self, candidates: Iterable[HarvestCandidate]) -> tuple[int, int]:
        inserted = 0
        updated = 0
        now = _now()
        with self.db:
            for candidate in candidates:
                if self._upsert_one(candidate, now=now):
                    inserted += 1
                else:
                    updated += 1
        return inserted, updated

    def upsert_many_with_cursor(
        self,
        candidates: Iterable[HarvestCandidate],
        *,
        source: str,
        cursor: str,
    ) -> tuple[int, int]:
        """Atomically persist one discovery page and its continuation cursor."""
        inserted = 0
        updated = 0
        now = _now()
        with self.db:
            for candidate in candidates:
                if self._upsert_one(candidate, now=now):
                    inserted += 1
                else:
                    updated += 1
            self._set_cursor_no_commit(source, cursor=cursor)
        return inserted, updated

    def apply_change_events(
        self,
        *,
        registry: str,
        source: str,
        events: Iterable[dict[str, Any]],
        cursor: str,
        priority: int = 60,
    ) -> dict[str, int]:
        """Apply an ordered change-feed batch and advance its cursor in one transaction.

        A crash before commit replays the whole response; a crash after commit resumes from
        the new cursor. Package updates/deletes keep their upstream order inside the batch.
        """
        inserted = 0
        updated = 0
        deleted = 0
        now = _now()
        with self.db:
            for event in events:
                name = str(event.get("name") or "").strip()
                if not name:
                    continue
                if bool(event.get("deleted")):
                    self._mark_deleted_no_commit(registry, name, source=source, now=now)
                    deleted += 1
                    continue
                candidate = HarvestCandidate(
                    registry,
                    name,
                    source,
                    priority,
                    requeue=True,
                )
                if self._upsert_one(candidate, now=now):
                    inserted += 1
                else:
                    updated += 1
            self._set_cursor_no_commit(source, cursor=cursor)
        return {"inserted": inserted, "updated": updated, "deleted": deleted}

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

    def mark_deleted(self, registry: str, name: str, *, source: str) -> None:
        with self.db:
            self._mark_deleted_no_commit(registry, name, source=source, now=_now())

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

    def cursor(self, source: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM cursors WHERE source=?", (source,)).fetchone()
        return dict(row) if row else {}

    def set_cursor(
        self,
        source: str,
        *,
        cursor: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        with self.db:
            self._set_cursor_no_commit(
                source,
                cursor=cursor,
                etag=etag,
                last_modified=last_modified,
            )

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

    def _headers(self, *, accept: str = "application/json", extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent, "Accept": accept}
        if extra:
            headers.update(extra)
        return headers

    def _get(self, url: str, *, headers: dict[str, str] | None = None, params: Any = None) -> requests.Response:
        response = self.session.get(
            url,
            headers=self._headers(extra=headers),
            params=params,
            timeout=120,
        )
        response.raise_for_status()
        return response

    def _npm_replication_info(self) -> tuple[str, dict[str, Any]]:
        last_error: Exception | None = None
        for url in NPM_REPLICATION_ROOTS:
            try:
                response = self.session.get(
                    url,
                    headers=self._headers(extra={"npm-replication-opt-in": "true"}),
                    timeout=120,
                )
                if int(getattr(response, "status_code", 0) or 0) == 404:
                    continue
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and payload.get("update_seq") is not None:
                    return url, payload
            except Exception as exc:
                last_error = exc
        if last_error:
            raise RuntimeError(f"npm replication root unavailable: {last_error}") from last_error
        raise RuntimeError("npm replication root returned no update_seq")

    def harvest_npm_full(self, *, limit: int = 500, reset: bool = False) -> dict[str, Any]:
        """Bootstrap the complete npm package-name universe via the official _all_docs API.

        The registry head sequence is captured before enumeration. Once enumeration is
        complete, incremental `_changes` starts from that captured sequence, so changes
        that happened during the bootstrap are replayed instead of being lost.
        """
        if reset:
            self.queue.reset_cursor(NPM_BOOTSTRAP_SOURCE)
            self.queue.reset_cursor(NPM_CHANGES_SOURCE)

        state_row = self.queue.cursor(NPM_BOOTSTRAP_SOURCE)
        state = _cursor_json(state_row.get("cursor"))
        if not state:
            root_url, info = self._npm_replication_info()
            snapshot_seq = str(info.get("update_seq"))
            state = {
                "complete": False,
                "snapshot_seq": snapshot_seq,
                "startkey": None,
                "seen_token": f"npm-bootstrap:{snapshot_seq}:{_now()}",
                "doc_count_at_start": int(info.get("doc_count") or 0),
                "root_url": root_url,
            }
            self.queue.set_cursor(NPM_BOOTSTRAP_SOURCE, cursor=_dump_cursor(state))

        snapshot_seq = str(state.get("snapshot_seq") or "").strip()
        if not snapshot_seq:
            raise RuntimeError("npm bootstrap cursor is missing snapshot_seq")

        if bool(state.get("complete")):
            changes = self.queue.cursor(NPM_CHANGES_SOURCE)
            if not str(changes.get("cursor") or "").strip():
                self.queue.set_cursor(NPM_CHANGES_SOURCE, cursor=snapshot_seq)
            return {
                "source": NPM_BOOTSTRAP_SOURCE,
                "registry": "npmjs.org",
                "full": True,
                "complete": True,
                "snapshot_seq": snapshot_seq,
                "changes_cursor": str(self.queue.cursor(NPM_CHANGES_SOURCE).get("cursor") or snapshot_seq),
                "discovered": 0,
                "inserted": 0,
                "updated": 0,
                "pages": 0,
            }

        target = None if int(limit) <= 0 else max(1, int(limit))
        discovered = 0
        inserted = 0
        updated = 0
        processed_rows = 0
        pages = 0
        total_rows = None
        startkey = state.get("startkey")
        seen_token = str(state.get("seen_token") or f"npm-bootstrap:{snapshot_seq}")
        complete = False

        while target is None or processed_rows < target:
            remaining = 1000 if target is None else min(1000, target - processed_rows)
            request_limit = remaining + (1 if startkey else 0)
            params: dict[str, Any] = {"limit": request_limit}
            if startkey:
                # CouchDB-style startkey is a JSON value, and npm's supported
                # replication endpoint keeps this pagination contract.
                params["startkey"] = json.dumps(str(startkey), ensure_ascii=False)
            response = self._get(
                NPM_ALL_DOCS_URL,
                headers={"npm-replication-opt-in": "true"},
                params=params,
            )
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("unexpected npm _all_docs payload")
            raw_rows = payload.get("rows") or []
            if not isinstance(raw_rows, list):
                raise ValueError("npm _all_docs rows is not a list")
            if total_rows is None and payload.get("total_rows") is not None:
                total_rows = int(payload.get("total_rows") or 0)

            rows = list(raw_rows)
            if startkey and rows and str((rows[0] or {}).get("id") or (rows[0] or {}).get("key") or "") == str(startkey):
                rows = rows[1:]
            if len(rows) > remaining:
                rows = rows[:remaining]

            page_candidates: list[HarvestCandidate] = []
            page_last_key = str(startkey or "") or None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_id = str(row.get("id") or row.get("key") or "").strip()
                if row_id:
                    page_last_key = row_id
                name = _npm_package_name(row_id)
                if not name:
                    continue
                page_candidates.append(
                    HarvestCandidate(
                        "npmjs.org",
                        name,
                        NPM_BOOTSTRAP_SOURCE,
                        15,
                        seen_token=seen_token,
                    )
                )

            exhausted = len(raw_rows) < request_limit or not rows
            next_state = {
                **state,
                "complete": bool(exhausted),
                "startkey": page_last_key,
                "last_page_rows": len(rows),
                "last_total_rows": total_rows,
            }
            page_inserted, page_updated = self.queue.upsert_many_with_cursor(
                page_candidates,
                source=NPM_BOOTSTRAP_SOURCE,
                cursor=_dump_cursor(next_state),
            )
            discovered += len(page_candidates)
            inserted += page_inserted
            updated += page_updated
            processed_rows += len(rows)
            pages += 1
            state = next_state
            startkey = page_last_key

            if exhausted:
                complete = True
                break
            if not rows:
                raise RuntimeError("npm _all_docs pagination stalled without advancing startkey")

        if complete:
            # Deliberately committed after the final _all_docs page. If the process dies
            # between these two writes, the next run reconstructs this cursor from the
            # completed bootstrap state instead of starting at an unsafe head position.
            if not str(self.queue.cursor(NPM_CHANGES_SOURCE).get("cursor") or "").strip():
                self.queue.set_cursor(NPM_CHANGES_SOURCE, cursor=snapshot_seq)

        return {
            "source": NPM_BOOTSTRAP_SOURCE,
            "registry": "npmjs.org",
            "full": True,
            "complete": complete,
            "snapshot_seq": snapshot_seq,
            "startkey": startkey,
            "doc_count_at_start": int(state.get("doc_count_at_start") or 0),
            "total_rows_last_response": total_rows,
            "processed_rows": processed_rows,
            "pages": pages,
            "discovered": discovered,
            "inserted": inserted,
            "updated": updated,
            "changes_cursor": str(self.queue.cursor(NPM_CHANGES_SOURCE).get("cursor") or "") or None,
        }

    def harvest_npm_changes(self, *, limit: int = 500, reset: bool = False) -> dict[str, Any]:
        """Follow the official npm change feed from the durable bootstrap sequence."""
        if reset:
            self.queue.reset_cursor(NPM_CHANGES_SOURCE)

        change_state = self.queue.cursor(NPM_CHANGES_SOURCE)
        since = str(change_state.get("cursor") or "").strip()
        bootstrap = _cursor_json(self.queue.cursor(NPM_BOOTSTRAP_SOURCE).get("cursor"))
        if not since and bootstrap.get("complete") and bootstrap.get("snapshot_seq") is not None:
            since = str(bootstrap["snapshot_seq"])
            self.queue.set_cursor(NPM_CHANGES_SOURCE, cursor=since)

        if not since:
            root_url, info = self._npm_replication_info()
            return {
                "source": NPM_CHANGES_SOURCE,
                "registry": "npmjs.org",
                "full": False,
                "bootstrap_required": True,
                "complete_bootstrap": bool(bootstrap.get("complete")),
                "current_update_seq": str(info.get("update_seq")),
                "current_doc_count": int(info.get("doc_count") or 0),
                "root_url": root_url,
                "discovered": 0,
                "inserted": 0,
                "updated": 0,
                "deleted": 0,
            }

        target = max(1, min(int(limit) if int(limit) > 0 else 5000, 5000))
        response = self._get(
            NPM_CHANGES_URL,
            headers={"npm-replication-opt-in": "true"},
            params={"since": since, "limit": target},
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("unexpected npm _changes payload")
        results = payload.get("results") or []
        if not isinstance(results, list):
            raise ValueError("npm _changes results is not a list")
        last_seq = payload.get("last_seq")
        if last_seq is None:
            raise ValueError("npm _changes response is missing last_seq")

        events: list[dict[str, Any]] = []
        non_package_rows = 0
        for row in results:
            if not isinstance(row, dict):
                continue
            name = _npm_package_name(row.get("id"))
            if not name:
                non_package_rows += 1
                continue
            events.append({"name": name, "deleted": bool(row.get("deleted"))})

        counts = self.queue.apply_change_events(
            registry="npmjs.org",
            source=NPM_CHANGES_SOURCE,
            events=events,
            cursor=str(last_seq),
            priority=60,
        )
        deleted = int(counts["deleted"])
        discovered = sum(1 for event in events if not event.get("deleted"))
        return {
            "source": NPM_CHANGES_SOURCE,
            "registry": "npmjs.org",
            "full": False,
            "bootstrap_required": False,
            "previous_cursor": since,
            "cursor": str(last_seq),
            "results": len(results),
            "events": len(events),
            "unique_packages": len({event["name"] for event in events}),
            "non_package_rows": non_package_rows,
            "discovered": discovered,
            "inserted": int(counts["inserted"]),
            "updated": int(counts["updated"]),
            "deleted": deleted,
            "pending": payload.get("pending"),
        }

    def harvest_packagist_popular(self, *, limit: int = 500) -> dict[str, Any]:
        candidates: list[HarvestCandidate] = []
        page = 1
        target = max(1, int(limit))
        while len(candidates) < target:
            response = self._get(
                "https://packagist.org/explore/popular.json",
                params={"per_page": min(100, target - len(candidates)), "page": page},
            )
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
        candidates: list[HarvestCandidate] = []
        for name in names:
            meta = package_map.get(name) or {}
            candidates.append(
                HarvestCandidate(
                    "packagist.org",
                    str(name),
                    "packagist-all",
                    20,
                    repository_hint=str(meta.get("repository")) if meta.get("repository") else None,
                    type_hint=str(meta.get("type")) if meta.get("type") else None,
                    abandoned=str(meta.get("abandoned")) if meta.get("abandoned") not in (None, False) else None,
                )
            )
        inserted, updated = self.queue.upsert_many(candidates)
        return {"source": "packagist-all", "discovered": len(candidates), "inserted": inserted, "updated": updated}

    def _packagist_changes_request(self, since: str | None) -> tuple[requests.Response, dict[str, Any]]:
        response = self.session.get(
            "https://packagist.org/metadata/changes.json",
            headers=self._headers(),
            params={"since": since} if since else None,
            timeout=120,
        )
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if int(getattr(response, "status_code", 200) or 200) == 400 and payload.get("timestamp") is not None:
            return response, payload
        response.raise_for_status()
        if not isinstance(payload, dict):
            raise ValueError("unexpected Packagist changes payload")
        return response, payload

    def harvest_packagist_changes(self, *, reset: bool = False) -> dict[str, Any]:
        source = "packagist-changes"
        if reset:
            self.queue.reset_cursor(source)
        state = self.queue.cursor(source)
        previous_cursor = str(state.get("cursor") or "").strip() or None
        response, payload = self._packagist_changes_request(previous_cursor)
        timestamp = payload.get("timestamp")
        status_code = int(getattr(response, "status_code", 200) or 200)

        if not previous_cursor and status_code == 400:
            self.queue.set_cursor(source, cursor=str(timestamp))
            return {
                "source": source,
                "initialized": True,
                "cursor": str(timestamp),
                "discovered": 0,
                "inserted": 0,
                "updated": 0,
                "deleted": 0,
                "resync_required": False,
            }

        if previous_cursor and status_code == 400:
            self.queue.set_cursor(source, cursor=str(timestamp))
            return {
                "source": source,
                "initialized": True,
                "cursor_reinitialized": True,
                "previous_cursor": previous_cursor,
                "cursor": str(timestamp),
                "discovered": 0,
                "inserted": 0,
                "updated": 0,
                "deleted": 0,
                "resync_required": True,
            }

        actions = payload.get("actions") or []
        candidates: list[HarvestCandidate] = []
        deleted = 0
        resync = False
        for action in actions:
            if not isinstance(action, dict):
                continue
            action_type = str(action.get("type") or "").casefold()
            if action_type == "resync":
                resync = True
                continue
            name = str(action.get("package") or "").replace("~dev", "").strip()
            if not name:
                continue
            if action_type == "delete":
                self.queue.mark_deleted("packagist.org", name, source=source)
                deleted += 1
                continue
            if action_type == "update":
                candidates.append(HarvestCandidate("packagist.org", name, source, 60, requeue=True))

        inserted, updated = self.queue.upsert_many(candidates)
        if timestamp is not None:
            self.queue.set_cursor(source, cursor=str(timestamp))
        return {
            "source": source,
            "initialized": False,
            "previous_cursor": previous_cursor,
            "cursor": str(timestamp) if timestamp is not None else previous_cursor,
            "actions": len(actions),
            "discovered": len(candidates),
            "inserted": inserted,
            "updated": updated,
            "deleted": deleted,
            "resync_required": resync,
        }

    def harvest_rubygems_activity(self, *, limit: int = 500) -> dict[str, Any]:
        candidates: dict[str, HarvestCandidate] = {}
        for endpoint, priority in (("latest", 45), ("just_updated", 55)):
            response = self._get(f"https://rubygems.org/api/v1/activity/{endpoint}.json")
            rows = response.json()
            for row in rows if isinstance(rows, list) else []:
                name = str((row or {}).get("name") or "").strip()
                if name:
                    candidates[name] = HarvestCandidate(
                        "rubygems.org",
                        name,
                        f"rubygems-{endpoint}",
                        priority,
                        requeue=endpoint == "just_updated",
                    )
        ordered = list(candidates.values())[: max(1, int(limit))] if limit > 0 else list(candidates.values())
        inserted, updated = self.queue.upsert_many(ordered)
        return {"source": "rubygems-activity", "discovered": len(ordered), "inserted": inserted, "updated": updated}

    def harvest_pubdev_popular(self, *, limit: int = 500, reset: bool = False) -> dict[str, Any]:
        source = "pubdev-popular"
        if reset:
            self.queue.reset_cursor(source)
        state = self.queue.cursor(source)
        extra = {"Accept-Encoding": "gzip"}
        if state.get("etag"):
            extra["If-None-Match"] = str(state["etag"])
        if state.get("last_modified"):
            extra["If-Modified-Since"] = str(state["last_modified"])
        response = self.session.get(
            "https://pub.dev/api/package-name-completion-data",
            headers=self._headers(extra=extra),
            timeout=120,
        )
        if int(getattr(response, "status_code", 200) or 200) == 304:
            return {
                "source": source,
                "ranked": True,
                "full": False,
                "not_modified": True,
                "complete": False,
                "discovered": 0,
                "inserted": 0,
                "updated": 0,
            }
        response.raise_for_status()
        payload = response.json()
        names = [str(name) for name in (payload.get("packages") or []) if str(name).strip()]
        target = max(1, int(limit))
        candidates = [HarvestCandidate("pub.dev", name, source, 50) for name in names[:target]]
        inserted, updated = self.queue.upsert_many(candidates)
        self.queue.set_cursor(
            source,
            cursor="RANKED",
            etag=(getattr(response, "headers", {}) or {}).get("ETag"),
            last_modified=(getattr(response, "headers", {}) or {}).get("Last-Modified"),
        )
        return {
            "source": source,
            "ranked": True,
            "full": False,
            "complete": False,
            "available_in_response": len(names),
            "discovered": len(candidates),
            "inserted": inserted,
            "updated": updated,
        }

    def harvest_pubdev_full(self, *, reset: bool = False) -> dict[str, Any]:
        source = "pubdev-package-names"
        if reset:
            self.queue.reset_cursor(source)
        state = self.queue.cursor(source)
        if state.get("cursor") == "__COMPLETE__":
            return {
                "source": source,
                "ranked": False,
                "full": True,
                "complete": True,
                "discovered": 0,
                "inserted": 0,
                "updated": 0,
            }

        next_url = str(state.get("cursor") or "https://pub.dev/api/package-names")
        discovered = 0
        inserted = 0
        updated = 0
        pages = 0
        while next_url:
            response = self._get(next_url, headers={"Accept-Encoding": "gzip"})
            payload = response.json()
            names = [str(name) for name in (payload.get("packages") or []) if str(name).strip()]
            page_candidates = [HarvestCandidate("pub.dev", name, source, 30) for name in names]
            page_inserted, page_updated = self.queue.upsert_many(page_candidates)
            discovered += len(page_candidates)
            inserted += page_inserted
            updated += page_updated
            pages += 1
            next_value = payload.get("nextUrl")
            next_url = str(next_value).strip() if next_value else ""
            self.queue.set_cursor(source, cursor=next_url or "__COMPLETE__")

        return {
            "source": source,
            "ranked": False,
            "full": True,
            "complete": True,
            "pages": pages,
            "discovered": discovered,
            "inserted": inserted,
            "updated": updated,
        }

    def harvest_pypi(self, *, limit: int = 1000, full: bool = False, reset: bool = False) -> dict[str, Any]:
        if not full:
            raise ValueError("PyPI full index enumeration is intentionally explicit; pass full=True")
        source = "pypi-simple"
        if reset:
            self.queue.reset_cursor(source)
        state = self.queue.cursor(source)
        headers = {"Accept": "application/vnd.pypi.simple.v1+json"}
        if state.get("etag"):
            headers["If-None-Match"] = str(state["etag"])
        if state.get("last_modified"):
            headers["If-Modified-Since"] = str(state["last_modified"])
        response = self.session.get(
            "https://pypi.org/simple/",
            headers={"User-Agent": self.user_agent, **headers},
            timeout=180,
        )
        if int(getattr(response, "status_code", 200) or 200) == 304:
            return {"source": source, "not_modified": True, "discovered": 0, "inserted": 0, "updated": 0}
        response.raise_for_status()
        payload = response.json()
        projects = payload.get("projects") or []
        if limit > 0:
            projects = projects[: int(limit)]
        candidates = [
            HarvestCandidate("pypi.org", str(row.get("name")), source, 10)
            for row in projects
            if isinstance(row, dict) and row.get("name")
        ]
        inserted, updated = self.queue.upsert_many(candidates)
        serial = (payload.get("meta") or {}).get("_last-serial") or (getattr(response, "headers", {}) or {}).get("X-PyPI-Last-Serial")
        self.queue.set_cursor(
            source,
            cursor=str(serial) if serial is not None else None,
            etag=(getattr(response, "headers", {}) or {}).get("ETag"),
            last_modified=(getattr(response, "headers", {}) or {}).get("Last-Modified"),
        )
        return {"source": source, "discovered": len(candidates), "inserted": inserted, "updated": updated, "serial": serial}

    def harvest(self, registry: str, *, limit: int = 500, full: bool = False, reset: bool = False) -> dict[str, Any]:
        key = registry.strip().casefold()
        if key in {"npm", "npmjs", "npmjs.org"}:
            return self.harvest_npm_full(limit=limit, reset=reset) if full else self.harvest_npm_changes(limit=limit, reset=reset)
        if key in {"packagist", "composer"}:
            return self.harvest_packagist_all(limit=limit) if full else self.harvest_packagist_popular(limit=limit)
        if key in {"packagist-changes", "composer-changes"}:
            return self.harvest_packagist_changes(reset=reset)
        if key in {"rubygems", "gem"}:
            return self.harvest_rubygems_activity(limit=limit)
        if key in {"pub", "pubdev", "dart"}:
            return self.harvest_pubdev_full(reset=reset) if full else self.harvest_pubdev_popular(limit=limit, reset=reset)
        if key in {"pypi", "python"}:
            return self.harvest_pypi(limit=limit, full=full, reset=reset)
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
        except Exception as exc:
            queue.mark_error(registry, name, str(exc))
            failed.append({"registry": registry, "name": name, "error": str(exc)[:500]})
    return {"selected": len(selected), "success": success, "failed": len(failed), "failures": failed[:50]}
