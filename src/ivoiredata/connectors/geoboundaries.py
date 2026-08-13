from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from ..snapshots import save_snapshot
from ..upstream_state import UpstreamState

_ADM_LEVELS = ("ADM0", "ADM1", "ADM2", "ADM3", "ADM4", "ADM5")


def _resolve_meta_urls(api_url: str) -> list[str]:
    parsed = urlparse(api_url)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/"
    last_segment = parsed.path.rstrip("/").rsplit("/", 1)[-1].upper()
    if last_segment not in _ADM_LEVELS:
        return [urljoin(base, lvl) + "/" for lvl in _ADM_LEVELS]
    return [api_url]


def _json_from_path(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def geoboundaries_resource(
    *,
    api_url: str,
    source_id: str = "civ_geoboundaries",
    user_agent: str = "IvoireData/0.8.3",
    snapshot_dir: Path | None = None,
    upstream_state_path: Path | None = None,
):
    """Synchronize geoBoundaries without partial replacement or false-304 loss.

    Aggregate metadata/features tables are rebuilt whenever any ADM level changed or an
    interrupted previous run must be completed. Changed levels come from the network and
    unchanged levels are replayed from persistent content-addressed snapshots. A 304 is
    therefore safe even if dlt state was not committed yet: cached metadata/GeoJSON is
    treated as changed relative to the missing dlt signature and is materialized locally.
    """
    import dlt
    import requests

    replay_dir = snapshot_dir
    if replay_dir is None and upstream_state_path is not None:
        replay_dir = upstream_state_path.parent / "upstream_cache" / source_id

    @dlt.resource(name="geoboundaries", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None
        state = dlt.current.resource_state()
        metadata_signatures = state.setdefault("metadata_signatures_v082", {})
        boundary_signatures = state.setdefault("boundary_signatures_v082", {})

        metadata_rows: list[dict[str, Any]] = []
        boundary_entries: list[dict[str, Any]] = []
        metadata_changed_any = False
        boundary_changed_any = False

        for meta_url in _resolve_meta_urls(api_url):
            level = meta_url.rstrip("/").rsplit("/", 1)[-1].upper()
            meta_artifact = f"metadata:{level}"
            cached_meta = upstream.get(source_id, meta_artifact) if upstream else {}
            headers = upstream.conditional_headers(source_id, meta_artifact) if upstream else {}
            meta_response = session.get(meta_url, timeout=120, headers=headers)
            if meta_response.status_code == 404:
                continue

            meta_changed = False
            if meta_response.status_code == 304:
                meta = cached_meta.get("metadata_payload")
                cached_digest = str(cached_meta.get("sha256") or cached_meta.get("signature") or "") or None
                if not isinstance(meta, (dict, list)):
                    meta_response = session.get(meta_url, timeout=120)
                    meta_response.raise_for_status()
                    meta = meta_response.json()
                    digest = hashlib.sha256(meta_response.content).hexdigest()
                    meta_changed = metadata_signatures.get(level) != digest
                    metadata_signatures[level] = digest
                    if upstream:
                        upstream.mark_downloaded(
                            source_id, meta_artifact, url=meta_response.url,
                            signature=digest, sha256=digest, size_bytes=len(meta_response.content),
                            etag=meta_response.headers.get("etag"),
                            last_modified=meta_response.headers.get("last-modified"),
                            method="HTTP_VALIDATORS+SHA256",
                            extra={"metadata_payload": meta},
                        )
                else:
                    # If dlt did not commit the preceding run, its level signature is
                    # missing/different. The cached metadata must then be emitted again.
                    meta_changed = metadata_signatures.get(level) != cached_digest
                    if cached_digest:
                        metadata_signatures[level] = cached_digest
                    if upstream:
                        upstream.mark_http_unchanged(
                            source_id, meta_artifact, url=meta_url,
                            extra={"signature": cached_digest, "metadata_payload": meta},
                        )
            else:
                meta_response.raise_for_status()
                ctype = meta_response.headers.get("content-type", "")
                if "html" in ctype.lower():
                    continue
                try:
                    meta = meta_response.json()
                except ValueError:
                    continue
                digest = hashlib.sha256(meta_response.content).hexdigest()
                meta_changed = metadata_signatures.get(level) != digest
                metadata_signatures[level] = digest
                if upstream:
                    upstream.mark_downloaded(
                        source_id, meta_artifact, url=meta_response.url,
                        signature=digest, sha256=digest, size_bytes=len(meta_response.content),
                        etag=meta_response.headers.get("etag"),
                        last_modified=meta_response.headers.get("last-modified"),
                        method="HTTP_VALIDATORS+SHA256",
                        extra={"metadata_payload": meta},
                    )

            metadata_changed_any = metadata_changed_any or meta_changed
            rows = meta if isinstance(meta, list) else [meta]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                metadata = dict(row)
                metadata["__ivoiredata_source_url"] = meta_url
                metadata["__ivoiredata_adm_level"] = level
                metadata_rows.append(metadata)

                download_url = (
                    row.get("gjDownloadURL") or row.get("gjDownloadUrl")
                    or row.get("geoJSON") or row.get("geojson")
                )
                if not isinstance(download_url, str) or not download_url:
                    continue

                url_key = hashlib.sha256(download_url.encode("utf-8")).hexdigest()[:16]
                boundary_artifact = f"geojson:{level}:{url_key}"
                cached_boundary = upstream.get(source_id, boundary_artifact) if upstream else {}
                cached_path = upstream.cached_path(source_id, boundary_artifact) if upstream else None
                boundary_headers = upstream.conditional_headers(source_id, boundary_artifact) if upstream else {}
                response = session.get(download_url, timeout=240, headers=boundary_headers)

                changed = False
                local_path: Path | None = cached_path
                digest: str | None = str(cached_boundary.get("sha256") or cached_boundary.get("signature") or "") or None

                if response.status_code == 304:
                    if local_path is None:
                        response = session.get(download_url, timeout=240)
                    else:
                        if digest is None:
                            digest = _sha256_path(local_path)
                        # Missing/different dlt state means the previous cached body was
                        # not committed. Rebuild the aggregate from cache without a body
                        # re-download.
                        changed = boundary_signatures.get(boundary_artifact) != digest
                        boundary_signatures[boundary_artifact] = digest
                        if upstream:
                            upstream.mark_http_unchanged(
                                source_id, boundary_artifact, url=download_url,
                                extra={"signature": digest, "local_path": str(local_path)},
                            )

                if response.status_code != 304:
                    response.raise_for_status()
                    digest = hashlib.sha256(response.content).hexdigest()
                    changed = boundary_signatures.get(boundary_artifact) != digest
                    snapshot = save_snapshot(
                        replay_dir,
                        source_id=source_id,
                        url=download_url,
                        content=response.content,
                        content_type=response.headers.get("content-type"),
                        name=f"{level}-{url_key}.geojson",
                    )
                    value = snapshot.get("local_path")
                    local_path = Path(str(value)) if value else None
                    boundary_signatures[boundary_artifact] = digest
                    if upstream:
                        if changed or not cached_boundary:
                            upstream.mark_downloaded(
                                source_id, boundary_artifact, url=download_url,
                                signature=digest, sha256=digest, size_bytes=len(response.content),
                                etag=response.headers.get("etag"),
                                last_modified=response.headers.get("last-modified"),
                                method="HTTP_VALIDATORS+SHA256",
                                local_path=str(local_path) if local_path else None,
                            )
                        else:
                            upstream.mark_unchanged(
                                source_id, boundary_artifact, signature=digest, url=download_url,
                                etag=response.headers.get("etag"),
                                last_modified=response.headers.get("last-modified"),
                                reason="SHA256",
                                extra={"local_path": str(local_path) if local_path else None},
                            )

                if local_path is None or not local_path.exists():
                    raise RuntimeError(f"geoBoundaries cache missing for {level}: {download_url}")
                boundary_changed_any = boundary_changed_any or changed
                boundary_entries.append({
                    "level": level,
                    "url": download_url,
                    "artifact": boundary_artifact,
                    "sha256": digest,
                    "local_path": local_path,
                })

        if metadata_changed_any:
            for row in metadata_rows:
                yield dlt.mark.with_table_name(row, "geoboundaries_metadata")

        if boundary_changed_any:
            for boundary_index, entry in enumerate(boundary_entries):
                payload = _json_from_path(entry["local_path"])
                features = payload.get("features", []) if isinstance(payload, dict) else []
                for feature_index, feature in enumerate(features):
                    if not isinstance(feature, dict):
                        continue
                    yield dlt.mark.with_table_name({
                        "feature_index": feature_index,
                        "feature_id": feature.get("id"),
                        "properties": feature.get("properties") or {},
                        "geometry": feature.get("geometry"),
                        "__ivoiredata_source_url": entry["url"],
                        "__ivoiredata_raw_sha256": entry["sha256"],
                        "__ivoiredata_raw_path": str(entry["local_path"]),
                        "__ivoiredata_boundary_index": boundary_index,
                        "__ivoiredata_adm_level": entry["level"],
                    }, "geoboundaries_features")

    return resource()