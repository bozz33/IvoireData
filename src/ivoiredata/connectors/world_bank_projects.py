from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..snapshots import save_snapshot
from ..state_io import atomic_write_json
from ..upstream_state import UpstreamState

SEARCH_API = "https://search.worldbank.org/api/v2/projects"


def _canonical_projects(projects: list[dict[str, Any]]) -> bytes:
    return json.dumps(projects, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def _cached_projects(upstream: UpstreamState | None, source_id: str, artifact: str):
    if upstream is None:
        return {}, None, None, None
    cached = upstream.get(source_id, artifact)
    path = upstream.cached_path(source_id, artifact)
    if path is None:
        return cached, None, None, None
    try:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        expected = str(cached.get("sha256") or cached.get("signature") or "").strip()
        if expected and expected != digest:
            return cached, None, None, None
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            return cached, None, None, None
        return cached, path, payload, digest
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return cached, None, None, None


def world_bank_projects_resource(
    *,
    country_code: str = "CI",
    page_size: int = 50,
    user_agent: str = "IvoireData/0.8.3",
    snapshot_dir: Path | None = None,
    upstream_state_path: Path | None = None,
):
    """Load World Bank Projects incrementally and recover interrupted loads locally.

    The first page uses HTTP validators when safe. A 304 is accepted directly only when
    dlt already committed the matching aggregate signature. If the preceding run crashed
    after download, the exact aggregate snapshot is replayed instead of downloading the
    same pages again. Without a valid replay cache, the request is retried unconditionally.
    """
    import dlt
    import requests

    page_size = max(1, min(int(page_size), 100))
    source_id = "civ_worldbank_projects"

    @dlt.resource(name="worldbank_projects", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None
        state = dlt.current.resource_state()
        committed_signature = state.get("content_signature")
        artifact = f"projects:{country_code}"
        cached, cached_path, replay_projects, replay_digest = _cached_projects(upstream, source_id, artifact)

        page = 1
        seen_ids: set[str] = set()
        all_projects: list[dict[str, Any]] = []
        first_response = None
        replayed_from_cache = False

        while True:
            params = {
                "countrycode_exact": country_code,
                "format": "json",
                "rows": page_size,
                "os": (page - 1) * page_size,
            }
            headers: dict[str, str] = {}
            if page == 1 and upstream and (
                (committed_signature and cached.get("signature") == committed_signature)
                or replay_projects is not None
            ):
                headers.update(upstream.conditional_headers(source_id, artifact))

            response = session.get(SEARCH_API, params=params, headers=headers, timeout=120)
            if page == 1 and response.status_code == 304:
                if committed_signature and cached.get("signature") == committed_signature:
                    stats = {
                        "country_code": country_code,
                        "unchanged": True,
                        "http_304": True,
                        "replayed_from_local_cache": False,
                        "projects": int(cached.get("rows") or 0),
                    }
                    if upstream:
                        upstream.mark_http_unchanged(
                            source_id, artifact, url=response.url,
                            extra={"signature": committed_signature, "local_path": str(cached_path) if cached_path else None},
                        )
                    if snapshot_dir:
                        atomic_write_json(snapshot_dir / "worldbank_projects_sync_stats.json", stats)
                    yield dlt.mark.with_table_name({"run_stats_json": json.dumps(stats, ensure_ascii=False), **stats}, "worldbank_projects_sync_stats")
                    return
                if replay_projects is not None and replay_digest is not None and cached_path is not None:
                    all_projects = [dict(item) for item in replay_projects]
                    replayed_from_cache = True
                    break
                # Never accept a network-only 304 when local materialization is absent.
                response = session.get(SEARCH_API, params=params, headers={}, timeout=120)

            response.raise_for_status()
            if first_response is None:
                first_response = response
            save_snapshot(
                snapshot_dir,
                source_id=source_id,
                url=response.url,
                content=response.content,
                content_type=response.headers.get("content-type"),
                name=f"projects-page-{page:04d}.json",
            )
            payload: Any = response.json()
            total = int(payload.get("total") or 0)
            projects = payload.get("projects") or []
            if isinstance(projects, dict):
                projects = list(projects.values())
            if not projects:
                break
            for project in projects:
                if not isinstance(project, dict):
                    continue
                pid = str(project.get("id") or "")
                if pid and pid in seen_ids:
                    continue
                if pid:
                    seen_ids.add(pid)
                all_projects.append(dict(project))
            if total and len(seen_ids) >= total:
                break
            if len(projects) < page_size:
                break
            page += 1

        canonical = _canonical_projects(all_projects)
        signature = hashlib.sha256(canonical).hexdigest()
        unchanged = committed_signature == signature
        stats = {
            "country_code": country_code,
            "unchanged": unchanged,
            "http_304": replayed_from_cache,
            "replayed_from_local_cache": replayed_from_cache,
            "projects": len(all_projects),
            "pages": 0 if replayed_from_cache else page,
            "content_sha256": signature,
        }

        aggregate_snapshot: dict[str, object]
        if replayed_from_cache and cached_path is not None:
            aggregate_snapshot = {
                "sha256": signature,
                "size_bytes": len(canonical),
                "source_url": str(cached.get("url") or SEARCH_API),
                "local_path": str(cached_path),
            }
        else:
            aggregate_snapshot = save_snapshot(
                snapshot_dir,
                source_id=source_id,
                url=SEARCH_API,
                content=canonical,
                content_type="application/json",
                name=f"projects-{country_code}-aggregate.json",
            )

        if not unchanged:
            for project in all_projects:
                row = dict(project)
                row["__ivoiredata_source_url"] = SEARCH_API
                row["__ivoiredata_country"] = country_code
                yield dlt.mark.with_table_name(row, "worldbank_projects")
            state["content_signature"] = signature

        if upstream:
            if replayed_from_cache:
                upstream.mark_downloaded(
                    source_id, artifact,
                    url=str(cached.get("url") or SEARCH_API),
                    signature=signature,
                    sha256=signature,
                    size_bytes=len(canonical),
                    etag=cached.get("etag"),
                    last_modified=cached.get("last_modified"),
                    method="CACHE_REPLAY_AFTER_304",
                    rows=len(all_projects),
                    local_path=str(aggregate_snapshot.get("local_path") or "") or None,
                )
            elif first_response is not None:
                upstream.mark_downloaded(
                    source_id, artifact,
                    url=first_response.url,
                    signature=signature,
                    sha256=signature,
                    size_bytes=len(canonical),
                    etag=first_response.headers.get("etag"),
                    last_modified=first_response.headers.get("last-modified"),
                    method="HTTP_VALIDATORS+CONTENT_HASH",
                    rows=len(all_projects),
                    local_path=str(aggregate_snapshot.get("local_path") or "") or None,
                )
        if snapshot_dir:
            atomic_write_json(snapshot_dir / "worldbank_projects_sync_stats.json", stats)
        yield dlt.mark.with_table_name({"run_stats_json": json.dumps(stats, ensure_ascii=False), **stats}, "worldbank_projects_sync_stats")

    return resource()