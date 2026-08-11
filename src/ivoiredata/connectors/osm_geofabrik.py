from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..upstream_state import UpstreamState


def _file_digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def geofabrik_snapshot_resource(
    *,
    page_url: str,
    output_dir: Path,
    source_id: str = "civ_osm_geofabrik",
    format: str = "pbf",
    user_agent: str = "IvoireData/0.8.2",
    upstream_state_path: Path | None = None,
):
    """Keep a local OpenStreetMap/Geofabrik snapshot without redundant transfers.

    Geofabrik publishes an MD5 sidecar for extracts. The sidecar is the preferred
    lightweight version signal. On the first v0.8.2 run, an existing v0.8.1 extract is
    hashed locally once and adopted if its MD5 equals the current official sidecar, so a
    large PBF is not downloaded merely to initialize the new cache.
    """
    import dlt
    import requests

    stem = page_url.rsplit(".html", 1)[0]
    choices = {
        "pbf": (f"{stem}-latest.osm.pbf", "ivory-coast-latest.osm.pbf"),
        "gpkg": (f"{stem}-latest-free.gpkg.zip", "ivory-coast-latest-free.gpkg.zip"),
        "shp": (f"{stem}-latest-free.shp.zip", "ivory-coast-latest-free.shp.zip"),
    }
    if format not in choices:
        raise ValueError(f"unsupported Geofabrik format: {format}")
    file_url, filename = choices[format]

    @dlt.resource(name="osm_geofabrik_snapshot", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent})
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / filename
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None
        artifact = f"extract:{format}"
        cached = upstream.get(source_id, artifact) if upstream else {}

        remote_md5 = None
        try:
            md5_response = session.get(file_url + ".md5", timeout=60)
            if md5_response.ok:
                candidate = md5_response.text.strip().split()[0].lower()
                if len(candidate) == 32 and all(ch in "0123456789abcdef" for ch in candidate):
                    remote_md5 = candidate
        except requests.RequestException:
            pass

        known_md5 = str(cached.get("remote_md5") or cached.get("md5") or "") or None
        local_md5 = known_md5 if path.exists() else None
        adopted_existing = False

        # One-time v0.8.1 -> v0.8.2 migration: compute the local checksum instead of
        # transferring the same large extract again merely because upstreams.json is new.
        if path.exists() and remote_md5 and not known_md5:
            local_md5 = _file_digest(path, "md5")
            if local_md5 == remote_md5:
                adopted_existing = True
                sha256 = _file_digest(path, "sha256")
                size = path.stat().st_size
                last_modified = None
                changed = False
                if upstream:
                    upstream.mark_downloaded(
                        source_id, artifact,
                        url=file_url,
                        signature=remote_md5,
                        sha256=sha256,
                        size_bytes=size,
                        method="ADOPTED_EXISTING_MD5",
                        local_path=str(path),
                        extra={"md5": local_md5, "remote_md5": remote_md5},
                    )

        if not adopted_existing and path.exists() and remote_md5 and known_md5 == remote_md5:
            changed = False
            sha256 = str(cached.get("sha256") or "") or None
            size = int(cached.get("size_bytes") or path.stat().st_size)
            last_modified = cached.get("last_modified")
            if upstream:
                upstream.mark_unchanged(
                    source_id, artifact, signature=remote_md5, url=file_url,
                    reason="GEOFABRIK_MD5", extra={"md5": local_md5, "remote_md5": remote_md5},
                )
        elif not adopted_existing:
            headers = upstream.conditional_headers(source_id, artifact) if upstream else {}
            response = session.get(file_url, timeout=900, stream=True, headers=headers)
            if response.status_code == 304 and path.exists():
                changed = False
                sha256 = str(cached.get("sha256") or "") or None
                size = int(cached.get("size_bytes") or path.stat().st_size)
                last_modified = cached.get("last_modified")
                if upstream:
                    upstream.mark_http_unchanged(
                        source_id, artifact, url=file_url,
                        extra={"md5": local_md5, "remote_md5": remote_md5},
                    )
            else:
                response.raise_for_status()
                temp = path.with_suffix(path.suffix + ".part")
                sha = hashlib.sha256()
                md5 = hashlib.md5()
                size = 0
                try:
                    with temp.open("wb") as handle:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            sha.update(chunk)
                            md5.update(chunk)
                            size += len(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp, path)
                finally:
                    temp.unlink(missing_ok=True)
                local_md5 = md5.hexdigest()
                sha256 = sha.hexdigest()
                last_modified = response.headers.get("last-modified")
                changed = True
                if upstream:
                    upstream.mark_downloaded(
                        source_id, artifact,
                        url=file_url,
                        signature=remote_md5 or response.headers.get("etag") or last_modified,
                        sha256=sha256,
                        size_bytes=size,
                        etag=response.headers.get("etag"),
                        last_modified=last_modified,
                        method="GEOFABRIK_MD5" if remote_md5 else "HTTP_VALIDATORS",
                        local_path=str(path),
                        extra={"md5": local_md5, "remote_md5": remote_md5},
                    )

        if sha256 is None and path.exists():
            sha256 = _file_digest(path, "sha256")
        if local_md5 is None and path.exists():
            local_md5 = _file_digest(path, "md5")

        yield {
            "source_id": source_id,
            "source_url": file_url,
            "format": format,
            "local_path": str(path),
            "size_bytes": size,
            "md5": local_md5,
            "remote_md5": remote_md5,
            "sha256": sha256,
            "changed": changed,
            "adopted_existing": adopted_existing,
            "last_modified": last_modified,
            "incremental_check": "MD5" if remote_md5 else "HTTP_VALIDATORS",
        }

    return resource()
