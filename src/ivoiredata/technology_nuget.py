from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

import requests


NUGET_SERVICE_INDEX_URL = "https://api.nuget.org/v3/index.json"
NUGET_REGISTRY = "nuget.org"
NUGET_BOOTSTRAP_SOURCE = "nuget-catalog-bootstrap"
NUGET_CHANGES_SOURCE = "nuget-catalog-changes"


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


def _type_values(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value]
    return [str(item or "").strip() for item in items if str(item or "").strip()]


def _leaf_kind(value: Any) -> str | None:
    values = _type_values(value)
    for item in values:
        tail = item.rsplit(":", 1)[-1]
        if tail == "PackageDetails":
            return "PackageDetails"
        if tail == "PackageDelete":
            return "PackageDelete"
    return None


def _item_key(item: dict[str, Any]) -> tuple[str, str]:
    return (
        str(item.get("commitTimeStamp") or item.get("catalog:commitTimeStamp") or ""),
        str(item.get("@id") or ""),
    )


def _package_id(item: dict[str, Any]) -> str:
    return str(item.get("nuget:id") or item.get("id") or "").strip()


def _package_version(item: dict[str, Any]) -> str:
    return str(item.get("nuget:version") or item.get("version") or "").strip()


def _page_sort_key(page: dict[str, Any]) -> tuple[str, str]:
    return (str(page.get("commitTimeStamp") or ""), str(page.get("@id") or ""))


@dataclass
class NuGetCatalogStats:
    processed_items: int = 0
    package_details: int = 0
    package_deletes: int = 0
    ignored_items: int = 0
    pages_fetched: int = 0
    inserted_packages: int = 0
    updated_packages: int = 0
    deleted_packages: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "processed_items": self.processed_items,
            "package_details": self.package_details,
            "package_deletes": self.package_deletes,
            "ignored_items": self.ignored_items,
            "pages_fetched": self.pages_fetched,
            "inserted_packages": self.inserted_packages,
            "updated_packages": self.updated_packages,
            "deleted_packages": self.deleted_packages,
        }


class NuGetCatalogHarvester:
    """Exhaustive and incremental package discovery from the official NuGet V3 Catalog.

    NuGet's catalog is chronological and append-only. We pin each bootstrap or incremental
    pass to the catalog index's commitTimeStamp/page count, and checkpoint the exact page
    and leaf key inside that snapshot. This permits bounded invocations without advancing
    past unprocessed items that share a commit timestamp.
    """

    def __init__(
        self,
        *,
        queue: Any,
        user_agent: str,
        session: requests.Session | None = None,
    ) -> None:
        self.queue = queue
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self._init_schema()

    def _init_schema(self) -> None:
        self.queue.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS nuget_version_state (
                package_id_norm TEXT NOT NULL,
                version_norm TEXT NOT NULL,
                package_id TEXT NOT NULL,
                version TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0,
                commit_timestamp TEXT NOT NULL,
                commit_id TEXT,
                leaf_url TEXT,
                leaf_type TEXT NOT NULL,
                PRIMARY KEY(package_id_norm, version_norm)
            );
            CREATE INDEX IF NOT EXISTS idx_nuget_version_package_deleted
                ON nuget_version_state(package_id_norm, deleted);
            """
        )
        self.queue.db.commit()

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept": "application/json"}

    def _get_json(self, url: str) -> dict[str, Any]:
        response = self.session.get(url, headers=self._headers(), timeout=120)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"unexpected NuGet JSON payload from {url}")
        return payload

    def _catalog_url(self) -> str:
        service = self._get_json(NUGET_SERVICE_INDEX_URL)
        fallback = None
        for resource in service.get("resources") or []:
            if not isinstance(resource, dict):
                continue
            rid = str(resource.get("@id") or "").strip()
            types = _type_values(resource.get("@type"))
            if not rid:
                continue
            if "Catalog/3.0.0" in types:
                return rid
            if any(item.startswith("Catalog/") for item in types):
                fallback = fallback or rid
        if fallback:
            return fallback
        raise ValueError("NuGet service index has no Catalog resource")

    @staticmethod
    def _sorted_pages(index_payload: dict[str, Any], *, snapshot_page_count: int) -> list[dict[str, Any]]:
        pages = [item for item in (index_payload.get("items") or []) if isinstance(item, dict) and item.get("@id")]
        pages.sort(key=_page_sort_key)
        if len(pages) < snapshot_page_count:
            raise RuntimeError(
                f"NuGet catalog page set shrank: expected at least {snapshot_page_count}, got {len(pages)}"
            )
        return pages[:snapshot_page_count]

    @staticmethod
    def _eligible_items(
        page_payload: dict[str, Any],
        *,
        after_timestamp: str | None,
        snapshot_timestamp: str,
        after_item_key: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for item in page_payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            timestamp, leaf_url = _item_key(item)
            if not timestamp or not leaf_url:
                continue
            if after_timestamp and timestamp <= after_timestamp:
                continue
            if timestamp > snapshot_timestamp:
                continue
            if after_item_key and (timestamp, leaf_url) <= after_item_key:
                continue
            items.append(item)
        items.sort(key=_item_key)
        return items

    def _apply_items_with_cursor(
        self,
        *,
        items: Iterable[dict[str, Any]],
        source: str,
        cursor_payload: dict[str, Any],
        requeue_changed: bool,
    ) -> NuGetCatalogStats:
        from .technology_harvester import HarvestCandidate, _now

        stats = NuGetCatalogStats()
        affected: dict[str, str] = {}
        with self.queue.db:
            for item in items:
                kind = _leaf_kind(item.get("@type"))
                package_id = _package_id(item)
                version = _package_version(item)
                timestamp, leaf_url = _item_key(item)
                if not kind or not package_id or not version or not timestamp:
                    stats.ignored_items += 1
                    continue
                package_norm = package_id.casefold()
                version_norm = version.casefold()
                deleted = 1 if kind == "PackageDelete" else 0
                self.queue.db.execute(
                    """
                    INSERT INTO nuget_version_state(
                        package_id_norm,version_norm,package_id,version,deleted,
                        commit_timestamp,commit_id,leaf_url,leaf_type
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(package_id_norm,version_norm) DO UPDATE SET
                        package_id=excluded.package_id,
                        version=excluded.version,
                        deleted=excluded.deleted,
                        commit_timestamp=excluded.commit_timestamp,
                        commit_id=excluded.commit_id,
                        leaf_url=excluded.leaf_url,
                        leaf_type=excluded.leaf_type
                    WHERE excluded.commit_timestamp >= nuget_version_state.commit_timestamp
                    """,
                    (
                        package_norm,
                        version_norm,
                        package_id,
                        version,
                        deleted,
                        timestamp,
                        str(item.get("commitId") or "") or None,
                        leaf_url,
                        kind,
                    ),
                )
                stats.processed_items += 1
                if kind == "PackageDelete":
                    stats.package_deletes += 1
                else:
                    stats.package_details += 1
                affected[package_norm] = package_id

            now = _now()
            inflight = cursor_payload.get("inflight") if isinstance(cursor_payload.get("inflight"), dict) else {}
            seen_token = str(cursor_payload.get("snapshot_timestamp") or inflight.get("target_timestamp") or "") or None
            for package_norm, _canonical_package_id in affected.items():
                active = self.queue.db.execute(
                    "SELECT COUNT(*) AS n FROM nuget_version_state WHERE package_id_norm=? AND deleted=0",
                    (package_norm,),
                ).fetchone()
                active_count = int(active["n"] or 0)
                previous = self.queue.db.execute(
                    "SELECT status FROM candidates WHERE registry=? AND name=? LIMIT 1",
                    (NUGET_REGISTRY, package_norm),
                ).fetchone()
                previous_status = str(previous["status"]) if previous else None
                if active_count <= 0:
                    self.queue._mark_deleted_no_commit(
                        NUGET_REGISTRY,
                        package_norm,
                        source=source,
                        now=now,
                    )
                    if previous_status != "DELETED":
                        stats.deleted_packages += 1
                    continue
                candidate = HarvestCandidate(
                    NUGET_REGISTRY,
                    package_norm,
                    source,
                    65 if requeue_changed else 25,
                    requeue=bool(requeue_changed or previous_status == "DELETED"),
                    seen_token=seen_token,
                )
                if self.queue._upsert_one(candidate, now=now):
                    stats.inserted_packages += 1
                else:
                    stats.updated_packages += 1

            self.queue._set_cursor_no_commit(source, cursor=_dump_cursor(cursor_payload))
        return stats

    def _registry_stats(self) -> dict[str, int]:
        candidate = self.queue.db.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN status='DELETED' THEN 1 ELSE 0 END) AS deleted,
              SUM(CASE WHEN status!='DELETED' THEN 1 ELSE 0 END) AS active
            FROM candidates WHERE registry=?
            """,
            (NUGET_REGISTRY,),
        ).fetchone()
        versions = self.queue.db.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN deleted=1 THEN 1 ELSE 0 END) AS deleted
            FROM nuget_version_state
            """
        ).fetchone()
        return {
            "registry_candidates": int(candidate["total"] or 0),
            "active_packages": int(candidate["active"] or 0),
            "deleted_packages": int(candidate["deleted"] or 0),
            "version_states": int(versions["total"] or 0),
            "deleted_versions": int(versions["deleted"] or 0),
        }

    def _initial_bootstrap_state(self) -> tuple[dict[str, Any], dict[str, Any]]:
        catalog_url = self._catalog_url()
        index_payload = self._get_json(catalog_url)
        snapshot_timestamp = str(index_payload.get("commitTimeStamp") or "").strip()
        snapshot_commit_id = str(index_payload.get("commitId") or "").strip()
        snapshot_page_count = int(index_payload.get("count") or len(index_payload.get("items") or []))
        if not snapshot_timestamp or snapshot_page_count <= 0:
            raise ValueError("NuGet catalog index is missing commitTimeStamp/count")
        state = {
            "complete": False,
            "catalog_url": catalog_url,
            "snapshot_timestamp": snapshot_timestamp,
            "snapshot_commit_id": snapshot_commit_id,
            "snapshot_page_count": snapshot_page_count,
            "page_index": 0,
            "after_item_key": None,
        }
        self.queue.set_cursor(NUGET_BOOTSTRAP_SOURCE, cursor=_dump_cursor(state))
        return state, index_payload

    def bootstrap(self, *, limit: int = 500, reset: bool = False) -> dict[str, Any]:
        if reset:
            self.queue.reset_cursor(NUGET_BOOTSTRAP_SOURCE)
            self.queue.reset_cursor(NUGET_CHANGES_SOURCE)
            with self.queue.db:
                self.queue.db.execute("DELETE FROM nuget_version_state")
                self.queue.db.execute("DELETE FROM candidates WHERE registry=?", (NUGET_REGISTRY,))

        state = _cursor_json(self.queue.cursor(NUGET_BOOTSTRAP_SOURCE).get("cursor"))
        if state.get("complete"):
            changes = _cursor_json(self.queue.cursor(NUGET_CHANGES_SOURCE).get("cursor"))
            if not changes:
                changes = {
                    "cursor_timestamp": str(state.get("snapshot_timestamp") or ""),
                    "cursor_commit_id": str(state.get("snapshot_commit_id") or ""),
                    "catalog_url": str(state.get("catalog_url") or ""),
                    "inflight": None,
                }
                self.queue.set_cursor(NUGET_CHANGES_SOURCE, cursor=_dump_cursor(changes))
            return {
                "source": NUGET_BOOTSTRAP_SOURCE,
                "registry": NUGET_REGISTRY,
                "full": True,
                "complete": True,
                "snapshot_timestamp": state.get("snapshot_timestamp"),
                "snapshot_commit_id": state.get("snapshot_commit_id"),
                "changes_cursor": changes.get("cursor_timestamp"),
                "processed_items": 0,
                "pages_fetched": 0,
                "http_work_required": False,
                **self._registry_stats(),
            }

        if not state:
            state, index_payload = self._initial_bootstrap_state()
        else:
            catalog_url = str(state.get("catalog_url") or "").strip()
            if not catalog_url:
                raise RuntimeError("NuGet bootstrap cursor is missing catalog_url")
            index_payload = self._get_json(catalog_url)

        snapshot_timestamp = str(state.get("snapshot_timestamp") or "").strip()
        snapshot_page_count = int(state.get("snapshot_page_count") or 0)
        if not snapshot_timestamp or snapshot_page_count <= 0:
            raise RuntimeError("NuGet bootstrap cursor is missing snapshot metadata")
        pages = self._sorted_pages(index_payload, snapshot_page_count=snapshot_page_count)
        target = None if int(limit) <= 0 else max(1, int(limit))
        aggregate = NuGetCatalogStats()
        page_index = max(0, int(state.get("page_index") or 0))
        after_raw = state.get("after_item_key")
        after_item_key = tuple(after_raw) if isinstance(after_raw, list) and len(after_raw) == 2 else None

        while page_index < len(pages):
            page = pages[page_index]
            page_payload = self._get_json(str(page["@id"]))
            aggregate.pages_fetched += 1
            items = self._eligible_items(
                page_payload,
                after_timestamp=None,
                snapshot_timestamp=snapshot_timestamp,
                after_item_key=after_item_key,
            )
            if target is not None:
                remaining = target - aggregate.processed_items
                if remaining <= 0:
                    break
                selected = items[:remaining]
            else:
                selected = items

            if selected:
                last_key = _item_key(selected[-1])
                finished_page = len(selected) == len(items)
                next_state = {
                    **state,
                    "page_index": page_index + 1 if finished_page else page_index,
                    "after_item_key": None if finished_page else list(last_key),
                }
                stats = self._apply_items_with_cursor(
                    items=selected,
                    source=NUGET_BOOTSTRAP_SOURCE,
                    cursor_payload=next_state,
                    requeue_changed=False,
                )
                for field, value in stats.as_dict().items():
                    setattr(aggregate, field, getattr(aggregate, field) + value)
                state = next_state
                if not finished_page:
                    break
            else:
                next_state = {**state, "page_index": page_index + 1, "after_item_key": None}
                self.queue.set_cursor(NUGET_BOOTSTRAP_SOURCE, cursor=_dump_cursor(next_state))
                state = next_state

            page_index = int(state.get("page_index") or 0)
            after_item_key = None
            if target is not None and aggregate.processed_items >= target:
                break

        complete = int(state.get("page_index") or 0) >= len(pages)
        if complete:
            state = {**state, "complete": True, "page_index": len(pages), "after_item_key": None}
            changes = {
                "cursor_timestamp": snapshot_timestamp,
                "cursor_commit_id": str(state.get("snapshot_commit_id") or ""),
                "catalog_url": str(state.get("catalog_url") or ""),
                "inflight": None,
            }
            with self.queue.db:
                self.queue._set_cursor_no_commit(NUGET_BOOTSTRAP_SOURCE, cursor=_dump_cursor(state))
                self.queue._set_cursor_no_commit(NUGET_CHANGES_SOURCE, cursor=_dump_cursor(changes))

        return {
            "source": NUGET_BOOTSTRAP_SOURCE,
            "registry": NUGET_REGISTRY,
            "full": True,
            "complete": complete,
            "snapshot_timestamp": snapshot_timestamp,
            "snapshot_commit_id": state.get("snapshot_commit_id"),
            "snapshot_page_count": snapshot_page_count,
            "page_index": state.get("page_index"),
            "after_item_key": state.get("after_item_key"),
            "changes_cursor": _cursor_json(self.queue.cursor(NUGET_CHANGES_SOURCE).get("cursor")).get("cursor_timestamp"),
            **aggregate.as_dict(),
            **self._registry_stats(),
        }

    def _new_incremental_inflight(self, changes: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        catalog_url = str(changes.get("catalog_url") or "").strip() or self._catalog_url()
        index_payload = self._get_json(catalog_url)
        target_timestamp = str(index_payload.get("commitTimeStamp") or "").strip()
        target_commit_id = str(index_payload.get("commitId") or "").strip()
        base_timestamp = str(changes.get("cursor_timestamp") or "").strip()
        if not base_timestamp:
            raise RuntimeError("NuGet changes cursor has no base timestamp")
        if target_timestamp <= base_timestamp:
            return changes, index_payload
        pages = self._sorted_pages(
            index_payload,
            snapshot_page_count=int(index_payload.get("count") or len(index_payload.get("items") or [])),
        )
        start_index = 0
        while start_index < len(pages) and str(pages[start_index].get("commitTimeStamp") or "") <= base_timestamp:
            start_index += 1
        inflight = {
            "base_timestamp": base_timestamp,
            "target_timestamp": target_timestamp,
            "target_commit_id": target_commit_id,
            "snapshot_page_count": len(pages),
            "page_index": start_index,
            "after_item_key": None,
        }
        changes = {**changes, "catalog_url": catalog_url, "inflight": inflight}
        self.queue.set_cursor(NUGET_CHANGES_SOURCE, cursor=_dump_cursor(changes))
        return changes, index_payload

    def changes(self, *, limit: int = 500, reset: bool = False) -> dict[str, Any]:
        bootstrap = _cursor_json(self.queue.cursor(NUGET_BOOTSTRAP_SOURCE).get("cursor"))
        if reset:
            self.queue.reset_cursor(NUGET_CHANGES_SOURCE)

        if not bootstrap.get("complete"):
            return {
                "source": NUGET_CHANGES_SOURCE,
                "registry": NUGET_REGISTRY,
                "full": False,
                "bootstrap_required": True,
                "complete_bootstrap": False,
                "processed_items": 0,
                "inserted_packages": 0,
                "updated_packages": 0,
                "deleted_packages": 0,
            }

        changes = _cursor_json(self.queue.cursor(NUGET_CHANGES_SOURCE).get("cursor"))
        if not changes:
            changes = {
                "cursor_timestamp": str(bootstrap.get("snapshot_timestamp") or ""),
                "cursor_commit_id": str(bootstrap.get("snapshot_commit_id") or ""),
                "catalog_url": str(bootstrap.get("catalog_url") or ""),
                "inflight": None,
            }
            self.queue.set_cursor(NUGET_CHANGES_SOURCE, cursor=_dump_cursor(changes))

        inflight = changes.get("inflight") if isinstance(changes.get("inflight"), dict) else None
        if inflight:
            index_payload = self._get_json(str(changes.get("catalog_url") or bootstrap.get("catalog_url")))
        else:
            changes, index_payload = self._new_incremental_inflight(changes)
            inflight = changes.get("inflight") if isinstance(changes.get("inflight"), dict) else None
            if not inflight:
                return {
                    "source": NUGET_CHANGES_SOURCE,
                    "registry": NUGET_REGISTRY,
                    "full": False,
                    "bootstrap_required": False,
                    "previous_cursor": changes.get("cursor_timestamp"),
                    "cursor": changes.get("cursor_timestamp"),
                    "target_timestamp": changes.get("cursor_timestamp"),
                    "processed_items": 0,
                    "pages_fetched": 0,
                    "inserted_packages": 0,
                    "updated_packages": 0,
                    "deleted_packages": 0,
                    **self._registry_stats(),
                }

        base_timestamp = str(inflight.get("base_timestamp") or "")
        target_timestamp = str(inflight.get("target_timestamp") or "")
        snapshot_page_count = int(inflight.get("snapshot_page_count") or 0)
        pages = self._sorted_pages(index_payload, snapshot_page_count=snapshot_page_count)
        page_index = max(0, int(inflight.get("page_index") or 0))
        after_raw = inflight.get("after_item_key")
        after_item_key = tuple(after_raw) if isinstance(after_raw, list) and len(after_raw) == 2 else None
        target = None if int(limit) <= 0 else max(1, int(limit))
        aggregate = NuGetCatalogStats()

        while page_index < len(pages):
            page = pages[page_index]
            page_payload = self._get_json(str(page["@id"]))
            aggregate.pages_fetched += 1
            items = self._eligible_items(
                page_payload,
                after_timestamp=base_timestamp,
                snapshot_timestamp=target_timestamp,
                after_item_key=after_item_key,
            )
            if target is not None:
                remaining = target - aggregate.processed_items
                if remaining <= 0:
                    break
                selected = items[:remaining]
            else:
                selected = items

            if selected:
                last_key = _item_key(selected[-1])
                finished_page = len(selected) == len(items)
                next_inflight = {
                    **inflight,
                    "page_index": page_index + 1 if finished_page else page_index,
                    "after_item_key": None if finished_page else list(last_key),
                }
                next_changes = {**changes, "inflight": next_inflight}
                stats = self._apply_items_with_cursor(
                    items=selected,
                    source=NUGET_CHANGES_SOURCE,
                    cursor_payload=next_changes,
                    requeue_changed=True,
                )
                for field, value in stats.as_dict().items():
                    setattr(aggregate, field, getattr(aggregate, field) + value)
                changes = next_changes
                inflight = next_inflight
                if not finished_page:
                    break
            else:
                next_inflight = {**inflight, "page_index": page_index + 1, "after_item_key": None}
                next_changes = {**changes, "inflight": next_inflight}
                self.queue.set_cursor(NUGET_CHANGES_SOURCE, cursor=_dump_cursor(next_changes))
                changes = next_changes
                inflight = next_inflight

            page_index = int(inflight.get("page_index") or 0)
            after_item_key = None
            if target is not None and aggregate.processed_items >= target:
                break

        complete_target = int(inflight.get("page_index") or 0) >= len(pages)
        previous_cursor = base_timestamp
        if complete_target:
            changes = {
                **changes,
                "cursor_timestamp": target_timestamp,
                "cursor_commit_id": str(inflight.get("target_commit_id") or ""),
                "inflight": None,
            }
            self.queue.set_cursor(NUGET_CHANGES_SOURCE, cursor=_dump_cursor(changes))

        return {
            "source": NUGET_CHANGES_SOURCE,
            "registry": NUGET_REGISTRY,
            "full": False,
            "bootstrap_required": False,
            "previous_cursor": previous_cursor,
            "cursor": changes.get("cursor_timestamp"),
            "target_timestamp": target_timestamp,
            "target_complete": complete_target,
            "inflight": changes.get("inflight"),
            **aggregate.as_dict(),
            **self._registry_stats(),
        }

    def harvest(self, *, full: bool = False, limit: int = 500, reset: bool = False) -> dict[str, Any]:
        return self.bootstrap(limit=limit, reset=reset) if full else self.changes(limit=limit, reset=reset)
