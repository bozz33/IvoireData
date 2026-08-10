from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..snapshots import save_snapshot
from ..state_io import atomic_write_json
from ..upstream_state import UpstreamState

SEARCH_API = "https://search.worldbank.org/api/v2/projects"


def world_bank_projects_resource(
    *,
    country_code: str = "CI",
    page_size: int = 50,
    user_agent: str = "IvoireData/0.8.2",
    snapshot_dir: Path | None = None,
    upstream_state_path: Path | None = None,
):
    """Load World Bank projects with conditional HTTP and aggregate content hashing."""
    import dlt
    import requests

    page_size = max(1, min(int(page_size), 100))

    @dlt.resource(name="worldbank_projects", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None
        state = dlt.current.resource_state()
        artifact = f"projects:{country_code}"
        cached = upstream.get("civ_worldbank_projects", artifact) if upstream else {}
        page = 1
        seen_ids: set[str] = set()
        all_projects: list[dict[str, Any]] = []
        first_response = None

        while True:
            params = {
                "countrycode_exact": country_code,
                "format": "json",
                "rows": page_size,
                "os": (page - 1) * page_size,
            }
            headers: dict[str, str] = {}
            if page == 1 and upstream:
                headers.update(upstream.conditional_headers("civ_worldbank_projects", artifact))
            response = session.get(SEARCH_API, params=params, headers=headers, timeout=120)
            if page == 1 and response.status_code == 304:
                stats = {
                    "country_code": country_code,
                    "unchanged": True,
                    "http_304": True,
                    "projects": int(cached.get("rows") or 0),
                }
                upstream.mark_http_unchanged("civ_worldbank_projects", artifact, url=response.url)
                if snapshot_dir:
                    atomic_write_json(snapshot_dir / "worldbank_projects_sync_stats.json", stats)
                yield dlt.mark.with_table_name({"run_stats_json": json.dumps(stats, ensure_ascii=False), **stats}, "worldbank_projects_sync_stats")
                return
            response.raise_for_status()
            if first_response is None:
                first_response = response
            save_snapshot(
                snapshot_dir,
                source_id="civ_worldbank_projects",
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

        canonical = json.dumps(all_projects, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        signature = hashlib.sha256(canonical).hexdigest()
        unchanged = state.get("content_signature") == signature
        stats = {
            "country_code": country_code,
            "unchanged": unchanged,
            "http_304": False,
            "projects": len(all_projects),
            "pages": page,
            "content_sha256": signature,
        }
        if not unchanged:
            for project in all_projects:
                row = dict(project)
                row["__ivoiredata_source_url"] = SEARCH_API
                row["__ivoiredata_country"] = country_code
                yield dlt.mark.with_table_name(row, "worldbank_projects")
            state["content_signature"] = signature

        if upstream and first_response is not None:
            upstream.mark_downloaded(
                "civ_worldbank_projects", artifact,
                url=first_response.url,
                signature=signature,
                sha256=signature,
                size_bytes=len(canonical),
                etag=first_response.headers.get("etag"),
                last_modified=first_response.headers.get("last-modified"),
                method="HTTP_VALIDATORS+CONTENT_HASH",
                rows=len(all_projects),
            )
        if snapshot_dir:
            atomic_write_json(snapshot_dir / "worldbank_projects_sync_stats.json", stats)
        yield dlt.mark.with_table_name({"run_stats_json": json.dumps(stats, ensure_ascii=False), **stats}, "worldbank_projects_sync_stats")

    return resource()
