from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import requests


GO_INDEX_URL = "https://index.golang.org/index"
GO_REGISTRY = "proxy.golang.org"
GO_BOOTSTRAP_SOURCE = "go-index-bootstrap"
GO_CHANGES_SOURCE = "go-index-changes"
GO_PAGE_LIMIT = 2000

_RFC3339_Z_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})(?:\.(?P<fraction>\d{1,9}))?Z$"
)


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


def _normalize_timestamp(value: Any) -> str:
    """Normalize RFC3339 timestamps to a lexicographically sortable UTC nanosecond form."""
    text = str(value or "").strip()
    match = _RFC3339_Z_RE.fullmatch(text)
    if match:
        fraction = (match.group("fraction") or "").ljust(9, "0")
        return f"{match.group('date')}T{match.group('time')}.{fraction}Z"
    if not text:
        return ""
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}000Z"


def _utc_now_timestamp(now_fn: Callable[[], datetime]) -> str:
    dt = now_fn()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}000Z"


def _record_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _normalize_timestamp(record.get("Timestamp")),
        str(record.get("Path") or "").strip(),
        str(record.get("Version") or "").strip(),
    )


@dataclass
class GoIndexStats:
    processed_versions: int = 0
    inserted_versions: int = 0
    replayed_versions: int = 0
    ignored_records: int = 0
    pages_fetched: int = 0
    inserted_modules: int = 0
    updated_modules: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "processed_versions": self.processed_versions,
            "inserted_versions": self.inserted_versions,
            "replayed_versions": self.replayed_versions,
            "ignored_records": self.ignored_records,
            "pages_fetched": self.pages_fetched,
            "inserted_modules": self.inserted_modules,
            "updated_modules": self.updated_modules,
        }


class GoModuleIndexHarvester:
    """Exhaustive + incremental discovery from the official Go module index.

    The public index is an ordered NDJSON feed of module versions. A bootstrap is pinned
    to a wall-clock snapshot boundary and uses include=all. The follower uses the same
    bounded snapshot/inflight model so an interrupted run never advances its global cursor
    past records that were not committed locally.

    `since` only accepts a timestamp, so we retain a secondary `(Timestamp, Path, Version)`
    key to deduplicate inclusive/replayed boundary records. If a full page cannot advance
    beyond that key, the harvester raises instead of skipping an ambiguous timestamp.
    """

    def __init__(
        self,
        *,
        queue: Any,
        user_agent: str,
        session: requests.Session | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.queue = queue
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._init_schema()

    def _init_schema(self) -> None:
        self.queue.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS go_module_version_state (
                module_path TEXT NOT NULL,
                version TEXT NOT NULL,
                first_cached_timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                PRIMARY KEY(module_path, version)
            );
            CREATE INDEX IF NOT EXISTS idx_go_version_timestamp
                ON go_module_version_state(first_cached_timestamp);
            """
        )
        self.queue.db.commit()

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept": "application/x-ndjson,text/plain"}

    def _fetch_page(self, *, since: str | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": GO_PAGE_LIMIT, "include": "all"}
        if since:
            params["since"] = since
        response = self.session.get(
            GO_INDEX_URL,
            headers=self._headers(),
            params=params,
            timeout=120,
        )
        response.raise_for_status()
        records: list[dict[str, Any]] = []
        for line in str(response.text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                records.append(payload)
        records.sort(key=_record_key)
        return records

    @staticmethod
    def _eligible_records(
        records: Iterable[dict[str, Any]],
        *,
        lower_timestamp: str | None,
        target_timestamp: str,
        after_key: tuple[str, str, str] | None,
    ) -> tuple[list[dict[str, Any]], bool]:
        eligible: list[dict[str, Any]] = []
        future_seen = False
        for record in records:
            key = _record_key(record)
            timestamp, path, version = key
            if not timestamp or not path or not version:
                continue
            if timestamp > target_timestamp:
                future_seen = True
                continue
            if lower_timestamp and timestamp <= lower_timestamp:
                continue
            if after_key and key <= after_key:
                continue
            eligible.append(record)
        eligible.sort(key=_record_key)
        return eligible, future_seen

    def _apply_records_with_cursor(
        self,
        *,
        records: Iterable[dict[str, Any]],
        source: str,
        cursor_payload: dict[str, Any],
        requeue_changed: bool,
    ) -> GoIndexStats:
        from .technology_harvester import HarvestCandidate, _now

        stats = GoIndexStats()
        affected: set[str] = set()
        seen_token = str(
            cursor_payload.get("snapshot_timestamp")
            or (cursor_payload.get("inflight") or {}).get("target_timestamp")
            or ""
        ) or None
        with self.queue.db:
            for record in records:
                timestamp, path, version = _record_key(record)
                if not timestamp or not path or not version:
                    stats.ignored_records += 1
                    continue
                cur = self.queue.db.execute(
                    """
                    INSERT OR IGNORE INTO go_module_version_state(
                        module_path,version,first_cached_timestamp,source
                    ) VALUES(?,?,?,?)
                    """,
                    (path, version, timestamp, source),
                )
                if int(cur.rowcount or 0) > 0:
                    stats.inserted_versions += 1
                else:
                    stats.replayed_versions += 1
                stats.processed_versions += 1
                affected.add(path)

            now = _now()
            for path in sorted(affected):
                candidate = HarvestCandidate(
                    GO_REGISTRY,
                    path,
                    source,
                    65 if requeue_changed else 25,
                    requeue=requeue_changed,
                    seen_token=seen_token,
                )
                if self.queue._upsert_one(candidate, now=now):
                    stats.inserted_modules += 1
                else:
                    stats.updated_modules += 1
            self.queue._set_cursor_no_commit(source, cursor=_dump_cursor(cursor_payload))
        return stats

    def _registry_stats(self) -> dict[str, int]:
        modules = self.queue.db.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE registry=?",
            (GO_REGISTRY,),
        ).fetchone()
        versions = self.queue.db.execute(
            "SELECT COUNT(*) AS n FROM go_module_version_state"
        ).fetchone()
        return {
            "registry_candidates": int(modules["n"] or 0),
            "version_states": int(versions["n"] or 0),
        }

    def _scan_snapshot(
        self,
        *,
        source: str,
        state: dict[str, Any],
        lower_timestamp: str | None,
        target_timestamp: str,
        limit: int,
        requeue_changed: bool,
        nested_inflight: bool,
    ) -> tuple[dict[str, Any], GoIndexStats, bool]:
        target = None if int(limit) <= 0 else max(1, int(limit))
        aggregate = GoIndexStats()
        complete = False

        while target is None or aggregate.processed_versions < target:
            working = state.get("inflight") if nested_inflight else state
            if not isinstance(working, dict):
                raise RuntimeError("Go index snapshot state is missing")
            since = str(working.get("since_timestamp") or "").strip() or None
            after_raw = working.get("after_key")
            after_key = (
                tuple(str(x) for x in after_raw)
                if isinstance(after_raw, list) and len(after_raw) == 3
                else None
            )
            records = self._fetch_page(since=since)
            aggregate.pages_fetched += 1
            eligible, future_seen = self._eligible_records(
                records,
                lower_timestamp=lower_timestamp,
                target_timestamp=target_timestamp,
                after_key=after_key,  # type: ignore[arg-type]
            )

            remaining = None if target is None else target - aggregate.processed_versions
            selected = eligible if remaining is None else eligible[: max(0, remaining)]
            response_exhausted = len(records) < GO_PAGE_LIMIT
            crossed_target = future_seen

            if selected:
                last_key = _record_key(selected[-1])
                next_working = {
                    **working,
                    "since_timestamp": last_key[0],
                    "after_key": list(last_key),
                }
                next_state = {**state, "inflight": next_working} if nested_inflight else next_working
                stats = self._apply_records_with_cursor(
                    records=selected,
                    source=source,
                    cursor_payload=next_state,
                    requeue_changed=requeue_changed,
                )
                for field, value in stats.as_dict().items():
                    setattr(aggregate, field, getattr(aggregate, field) + value)
                state = next_state

                cut_by_invocation_limit = len(selected) < len(eligible)
                if cut_by_invocation_limit:
                    break
                if response_exhausted or crossed_target:
                    complete = True
                    break
                if target is not None and aggregate.processed_versions >= target:
                    break
                continue

            if response_exhausted or crossed_target:
                complete = True
                break

            # A full response that contains no key after our saved boundary means the
            # timestamp-only upstream cursor cannot safely move forward. Never guess by
            # adding an arbitrary nanosecond: that could skip legitimate records.
            max_key = max((_record_key(record) for record in records), default=("", "", ""))
            if after_key and max_key <= after_key:
                raise RuntimeError(
                    "Go index cursor stalled on a full page at one timestamp; refusing unsafe cursor advance"
                )
            if lower_timestamp and max_key[0] <= lower_timestamp:
                raise RuntimeError(
                    "Go index incremental cursor cannot advance beyond its timestamp boundary safely"
                )
            raise RuntimeError("Go index returned a full page without an eligible cursor advance")

        return state, aggregate, complete

    def bootstrap(self, *, limit: int = 500, reset: bool = False) -> dict[str, Any]:
        if reset:
            self.queue.reset_cursor(GO_BOOTSTRAP_SOURCE)
            self.queue.reset_cursor(GO_CHANGES_SOURCE)
            with self.queue.db:
                self.queue.db.execute("DELETE FROM go_module_version_state")
                self.queue.db.execute("DELETE FROM candidates WHERE registry=?", (GO_REGISTRY,))

        state = _cursor_json(self.queue.cursor(GO_BOOTSTRAP_SOURCE).get("cursor"))
        if state.get("complete"):
            changes = _cursor_json(self.queue.cursor(GO_CHANGES_SOURCE).get("cursor"))
            if not changes:
                changes = {
                    "cursor_timestamp": str(state.get("snapshot_timestamp") or ""),
                    "inflight": None,
                }
                self.queue.set_cursor(GO_CHANGES_SOURCE, cursor=_dump_cursor(changes))
            return {
                "source": GO_BOOTSTRAP_SOURCE,
                "registry": GO_REGISTRY,
                "full": True,
                "complete": True,
                "snapshot_timestamp": state.get("snapshot_timestamp"),
                "changes_cursor": changes.get("cursor_timestamp"),
                "processed_versions": 0,
                "pages_fetched": 0,
                "http_work_required": False,
                **self._registry_stats(),
            }

        if not state:
            state = {
                "complete": False,
                "snapshot_timestamp": _utc_now_timestamp(self.now_fn),
                "since_timestamp": None,
                "after_key": None,
            }
            self.queue.set_cursor(GO_BOOTSTRAP_SOURCE, cursor=_dump_cursor(state))

        snapshot = str(state.get("snapshot_timestamp") or "").strip()
        if not snapshot:
            raise RuntimeError("Go bootstrap cursor is missing snapshot_timestamp")

        state, aggregate, complete = self._scan_snapshot(
            source=GO_BOOTSTRAP_SOURCE,
            state=state,
            lower_timestamp=None,
            target_timestamp=snapshot,
            limit=limit,
            requeue_changed=False,
            nested_inflight=False,
        )
        if complete:
            state = {**state, "complete": True}
            changes = {"cursor_timestamp": snapshot, "inflight": None}
            with self.queue.db:
                self.queue._set_cursor_no_commit(GO_BOOTSTRAP_SOURCE, cursor=_dump_cursor(state))
                self.queue._set_cursor_no_commit(GO_CHANGES_SOURCE, cursor=_dump_cursor(changes))

        return {
            "source": GO_BOOTSTRAP_SOURCE,
            "registry": GO_REGISTRY,
            "full": True,
            "complete": complete,
            "snapshot_timestamp": snapshot,
            "since_timestamp": state.get("since_timestamp"),
            "after_key": state.get("after_key"),
            "changes_cursor": _cursor_json(self.queue.cursor(GO_CHANGES_SOURCE).get("cursor")).get("cursor_timestamp"),
            **aggregate.as_dict(),
            **self._registry_stats(),
        }

    def changes(self, *, limit: int = 500, reset: bool = False) -> dict[str, Any]:
        bootstrap = _cursor_json(self.queue.cursor(GO_BOOTSTRAP_SOURCE).get("cursor"))
        if reset:
            self.queue.reset_cursor(GO_CHANGES_SOURCE)

        if not bootstrap.get("complete"):
            return {
                "source": GO_CHANGES_SOURCE,
                "registry": GO_REGISTRY,
                "full": False,
                "bootstrap_required": True,
                "complete_bootstrap": False,
                "processed_versions": 0,
                "inserted_modules": 0,
                "updated_modules": 0,
            }

        changes = _cursor_json(self.queue.cursor(GO_CHANGES_SOURCE).get("cursor"))
        if not changes:
            changes = {
                "cursor_timestamp": str(bootstrap.get("snapshot_timestamp") or ""),
                "inflight": None,
            }
            self.queue.set_cursor(GO_CHANGES_SOURCE, cursor=_dump_cursor(changes))

        inflight = changes.get("inflight") if isinstance(changes.get("inflight"), dict) else None
        if not inflight:
            base = str(changes.get("cursor_timestamp") or "").strip()
            if not base:
                raise RuntimeError("Go changes cursor has no base timestamp")
            target = _utc_now_timestamp(self.now_fn)
            if target <= base:
                return {
                    "source": GO_CHANGES_SOURCE,
                    "registry": GO_REGISTRY,
                    "full": False,
                    "bootstrap_required": False,
                    "previous_cursor": base,
                    "cursor": base,
                    "target_timestamp": base,
                    "target_complete": True,
                    "inflight": None,
                    "processed_versions": 0,
                    "pages_fetched": 0,
                    "inserted_modules": 0,
                    "updated_modules": 0,
                    **self._registry_stats(),
                }
            inflight = {
                "base_timestamp": base,
                "target_timestamp": target,
                "since_timestamp": base,
                "after_key": None,
            }
            changes = {**changes, "inflight": inflight}
            self.queue.set_cursor(GO_CHANGES_SOURCE, cursor=_dump_cursor(changes))

        base = str(inflight.get("base_timestamp") or "").strip()
        target = str(inflight.get("target_timestamp") or "").strip()
        if not base or not target:
            raise RuntimeError("Go incremental inflight cursor is incomplete")

        changes, aggregate, complete = self._scan_snapshot(
            source=GO_CHANGES_SOURCE,
            state=changes,
            lower_timestamp=base,
            target_timestamp=target,
            limit=limit,
            requeue_changed=True,
            nested_inflight=True,
        )
        previous_cursor = base
        if complete:
            changes = {**changes, "cursor_timestamp": target, "inflight": None}
            self.queue.set_cursor(GO_CHANGES_SOURCE, cursor=_dump_cursor(changes))

        return {
            "source": GO_CHANGES_SOURCE,
            "registry": GO_REGISTRY,
            "full": False,
            "bootstrap_required": False,
            "previous_cursor": previous_cursor,
            "cursor": changes.get("cursor_timestamp"),
            "target_timestamp": target,
            "target_complete": complete,
            "inflight": changes.get("inflight"),
            **aggregate.as_dict(),
            **self._registry_stats(),
        }

    def harvest(self, *, full: bool = False, limit: int = 500, reset: bool = False) -> dict[str, Any]:
        return self.bootstrap(limit=limit, reset=reset) if full else self.changes(limit=limit, reset=reset)
