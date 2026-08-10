from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from ..upstream_state import UpstreamState

_ADM_LEVELS = ("ADM0", "ADM1", "ADM2", "ADM3", "ADM4", "ADM5")


def _resolve_meta_urls(api_url: str) -> list[str]:
    parsed = urlparse(api_url)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/"
    last_segment = parsed.path.rstrip("/").rsplit("/", 1)[-1].upper()
    if last_segment not in _ADM_LEVELS:
        return [urljoin(base, lvl) + "/" for lvl in _ADM_LEVELS]
    return [api_url]


def geoboundaries_resource(
    *,
    api_url: str,
    source_id: str = "civ_geoboundaries",
    user_agent: str = "IvoireData/0.8.2",
    upstream_state_path: Path | None = None,
):
    """Synchronize geoBoundaries using HTTP validators for metadata and GeoJSON."""
    import dlt
    import requests

    @dlt.resource(name="geoboundaries", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None
        state = dlt.current.resource_state()
        metadata_signatures = state.setdefault("metadata_signatures_v082", {})
        boundary_signatures = state.setdefault("boundary_signatures_v082", {})
        boundary_index = 0

        for meta_url in _resolve_meta_urls(api_url):
            level = meta_url.rstrip("/").rsplit("/", 1)[-1].upper()
            meta_artifact = f"metadata:{level}"
            cached_meta = upstream.get(source_id, meta_artifact) if upstream else {}
            headers = upstream.conditional_headers(source_id, meta_artifact) if upstream else {}
            meta_response = session.get(meta_url, timeout=120, headers=headers)
            if meta_response.status_code == 404:
                continue
            if meta_response.status_code == 304:
                meta = cached_meta.get("metadata_payload")
                if upstream:
                    upstream.mark_http_unchanged(source_id, meta_artifact, url=meta_url)
                if not isinstance(meta, (dict, list)):
                    continue
                rows = meta if isinstance(meta, list) else [meta]
            else:
                meta_response.raise_for_status()
                ctype = meta_response.headers.get("content-type", "")
                if "html" in ctype.lower():
                    continue
                try:
                    meta = meta_response.json()
                except ValueError:
                    continue
                rows = meta if isinstance(meta, list) else [meta]
                digest = hashlib.sha256(meta_response.content).hexdigest()
                metadata_changed = metadata_signatures.get(level) != digest
                if metadata_changed:
                    for row in rows:
                        if isinstance(row, dict):
                            metadata = dict(row)
                            metadata["__ivoiredata_source_url"] = meta_url
                            yield dlt.mark.with_table_name(metadata, "geoboundaries_metadata")
                    metadata_signatures[level] = digest
                if upstream:
                    upstream.mark_downloaded(
                        source_id, meta_artifact, url=meta_response.url, signature=digest,
                        sha256=digest, size_bytes=len(meta_response.content),
                        etag=meta_response.headers.get("etag"), last_modified=meta_response.headers.get("last-modified"),
                        method="HTTP_VALIDATORS+SHA256",
                        extra={"metadata_payload": meta},
                    )

            for row in rows:
                if not isinstance(row, dict):
                    continue
                download_url = row.get("gjDownloadURL") or row.get("gjDownloadUrl") or row.get("geoJSON") or row.get("geojson")
                if not isinstance(download_url, str) or not download_url:
                    continue
                boundary_artifact = f"geojson:{level}:{boundary_index}"
                headers = upstream.conditional_headers(source_id, boundary_artifact) if upstream else {}
                response = session.get(download_url, timeout=180, headers=headers)
                if response.status_code == 304:
                    if upstream:
                        upstream.mark_http_unchanged(source_id, boundary_artifact, url=download_url)
                    boundary_index += 1
                    continue
                response.raise_for_status()
                digest = hashlib.sha256(response.content).hexdigest()
                if boundary_signatures.get(boundary_artifact) == digest:
                    if upstream:
                        upstream.mark_unchanged(
                            source_id, boundary_artifact, signature=digest, url=download_url,
                            etag=response.headers.get("etag"), last_modified=response.headers.get("last-modified"), reason="SHA256",
                        )
                    boundary_index += 1
                    continue
                payload: Any = response.json()
                features = payload.get("features", []) if isinstance(payload, dict) else []
                for feature_index, feature in enumerate(features):
                    if not isinstance(feature, dict):
                        continue
                    item = {
                        "feature_index": feature_index,
                        "feature_id": feature.get("id"),
                        "properties": feature.get("properties") or {},
                        "geometry": feature.get("geometry"),
                        "__ivoiredata_source_url": download_url,
                        "__ivoiredata_raw_sha256": digest,
                        "__ivoiredata_boundary_index": boundary_index,
                        "__ivoiredata_adm_level": level,
                    }
                    yield dlt.mark.with_table_name(item, "geoboundaries_features")
                boundary_signatures[boundary_artifact] = digest
                if upstream:
                    upstream.mark_downloaded(
                        source_id, boundary_artifact, url=download_url, signature=digest,
                        sha256=digest, size_bytes=len(response.content), etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"), method="HTTP_VALIDATORS+SHA256",
                        rows=len(features),
                    )
                boundary_index += 1

    return resource()
