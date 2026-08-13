from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..snapshots import save_snapshot
from ..state_io import atomic_write_json
from ..upstream_state import UpstreamState

API = "https://api.uis.unesco.org/api/public"


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "items", "records", "value"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if payload and all(isinstance(value, dict) for value in payload.values()):
            return [dict(value) for value in payload.values()]
    return []


def _cached_json(upstream: UpstreamState | None, source_id: str, artifact: str):
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
        return cached, path, json.loads(raw.decode("utf-8-sig")), digest
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return cached, None, None, None


def _conditional_json(session, url: str, *, source_id: str, artifact: str,
                      upstream: UpstreamState | None, committed_signature: str | None,
                      params: list[tuple[str, str]] | dict[str, Any] | None = None,
                      timeout: int = 180):
    cached, cached_path, cached_payload, cached_digest = _cached_json(upstream, source_id, artifact)
    headers = {"Accept": "application/json"}
    if upstream and (
        (committed_signature and cached.get("signature") == committed_signature)
        or cached_payload is not None
    ):
        headers.update(upstream.conditional_headers(source_id, artifact))

    response = session.get(url, params=params, headers=headers, timeout=timeout)
    if response.status_code == 304:
        if committed_signature and cached.get("signature") == committed_signature:
            return response, None, None, None, cached
        if cached_payload is not None and cached_path is not None and cached_digest is not None:
            return response, cached_payload, cached_digest, cached_path, cached
        # We cannot accept a 304 unless the exact body is committed or replayable.
        response = session.get(url, params=params, headers={"Accept": "application/json"}, timeout=timeout)

    response.raise_for_status()
    digest = hashlib.sha256(response.content).hexdigest()
    return response, response.json(), digest, None, cached


def uis_country_resource(
    *,
    geo_unit: str = "CIV",
    start_year: int | None = None,
    end_year: int | None = None,
    user_agent: str = "IvoireData/0.8.3",
    snapshot_dir: Path | None = None,
    upstream_state_path: Path | None = None,
):
    """Load UNESCO UIS data through the official public API without duplicate bodies.

    Each artifact uses ETag/Last-Modified when available and SHA-256 otherwise. A 304 is
    trusted only when dlt proves the matching digest is already committed, or when the
    exact cached body passes an integrity check and can be replayed to finish an
    interrupted materialization.
    """
    import dlt
    import requests

    @dlt.resource(name="uis_civ", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent})
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None
        signatures = dlt.current.resource_state().setdefault("artifact_signatures_v082", {})
        stats: dict[str, Any] = {
            "geo_unit": geo_unit,
            "artifacts_checked": 0,
            "unchanged": 0,
            "updated": 0,
            "replayed_from_local_cache": 0,
            "data_rows": 0,
        }

        def handle(artifact: str, url: str, table: str, *, params=None, filter_geo: bool = False, required: bool = False):
            stats["artifacts_checked"] += 1
            committed = signatures.get(artifact)
            response, payload, digest, replay_path, cached = _conditional_json(
                session, url, source_id="civ_uis", artifact=artifact,
                upstream=upstream, committed_signature=committed,
                params=params, timeout=240 if artifact == "data" else 180,
            )
            if response.status_code == 304 and digest is None:
                stats["unchanged"] += 1
                if upstream:
                    upstream.mark_http_unchanged(
                        "civ_uis", artifact, url=response.url,
                        extra={"signature": committed, "local_path": str(cached.get("local_path") or "") or None},
                    )
                return []

            assert digest is not None and payload is not None
            if committed == digest:
                stats["unchanged"] += 1
                if upstream:
                    upstream.mark_unchanged(
                        "civ_uis", artifact, signature=digest, url=response.url,
                        etag=response.headers.get("etag") or cached.get("etag"),
                        last_modified=response.headers.get("last-modified") or cached.get("last_modified"),
                        reason="SHA256",
                    )
                return []

            if replay_path is not None:
                snapshot = {
                    "sha256": digest,
                    "size_bytes": replay_path.stat().st_size,
                    "source_url": str(cached.get("url") or url),
                    "local_path": str(replay_path),
                }
                stats["replayed_from_local_cache"] += 1
            else:
                snapshot = save_snapshot(
                    snapshot_dir,
                    source_id="civ_uis",
                    url=response.url,
                    content=response.content,
                    content_type=response.headers.get("content-type"),
                    name=f"uis-{artifact}.json",
                )

            rows = _rows(payload)
            if required and not rows:
                raise RuntimeError(f"UIS API returned no rows for {artifact} geoUnit={geo_unit}")
            out: list[dict[str, Any]] = []
            for row in rows:
                if filter_geo:
                    code = str(row.get("geoUnitCode") or row.get("code") or row.get("id") or row.get("iso3") or "")
                    if code and code.upper() != geo_unit.upper():
                        continue
                item = dict(row)
                item["__ivoiredata_source_url"] = str(cached.get("url") or response.url) if replay_path else response.url
                item["__ivoiredata_raw_sha256"] = snapshot["sha256"]
                item["__ivoiredata_raw_path"] = snapshot.get("local_path")
                if artifact == "data":
                    item["__ivoiredata_geo_unit"] = geo_unit
                out.append(item)

            signatures[artifact] = digest
            stats["updated"] += 1
            if artifact == "data":
                stats["data_rows"] = len(out)
            if upstream:
                upstream.mark_downloaded(
                    "civ_uis", artifact,
                    url=str(cached.get("url") or response.url) if replay_path else response.url,
                    signature=digest,
                    sha256=digest,
                    size_bytes=int(snapshot.get("size_bytes") or 0),
                    etag=response.headers.get("etag") or cached.get("etag"),
                    last_modified=response.headers.get("last-modified") or cached.get("last_modified"),
                    method="CACHE_REPLAY_AFTER_304" if replay_path else "HTTP_VALIDATORS+SHA256",
                    rows=len(out), local_path=str(snapshot.get("local_path") or "") or None,
                )
            return [(table, item) for item in out]

        definitions_url = f"{API}/definitions/indicators"
        for table, item in handle("indicator_definitions", definitions_url, "uis_indicators"):
            yield dlt.mark.with_table_name(item, table)

        geounits_url = f"{API}/definitions/geounits"
        for table, item in handle("geounits", geounits_url, "uis_geounits", filter_geo=True):
            yield dlt.mark.with_table_name(item, table)

        params: list[tuple[str, str]] = [("geoUnit", geo_unit)]
        if start_year is not None:
            params.append(("startYear", str(int(start_year))))
        if end_year is not None:
            params.append(("endYear", str(int(end_year))))
        data_url = f"{API}/data/indicators"
        for table, item in handle("data", data_url, "uis_data", params=params, required=True):
            yield dlt.mark.with_table_name(item, table)

        if snapshot_dir:
            atomic_write_json(snapshot_dir / "uis_sync_stats.json", stats)
        yield dlt.mark.with_table_name({"run_stats_json": json.dumps(stats, ensure_ascii=False), **stats}, "uis_sync_stats")

    return resource()