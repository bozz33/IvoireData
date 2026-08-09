from __future__ import annotations

import hashlib
from pathlib import Path


def geofabrik_snapshot_resource(*, page_url: str, output_dir: Path, source_id: str = "civ_osm_geofabrik", format: str = "pbf", user_agent: str = "IvoireData/0.5"):
    """Keep a local OpenStreetMap/Geofabrik snapshot for Côte d'Ivoire.

    Supported formats are pbf (preferred), gpkg and shp. The binary is stored
    outside Git in data_lake/raw_external and only replaced when the remote
    checksum changes.
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
        session = requests.Session(); session.headers.update({"User-Agent": user_agent})
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / filename
        remote_md5 = None
        try:
            md5_response = session.get(file_url + ".md5", timeout=60)
            if md5_response.ok:
                remote_md5 = md5_response.text.strip().split()[0].lower()
        except requests.RequestException:
            pass
        local_md5 = hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else None
        changed = not path.exists() or remote_md5 is None or local_md5 != remote_md5
        if changed:
            response = session.get(file_url, timeout=900, stream=True); response.raise_for_status()
            temp = path.with_suffix(path.suffix + ".part")
            sha = hashlib.sha256(); md5 = hashlib.md5(); size = 0
            with temp.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk: continue
                    handle.write(chunk); sha.update(chunk); md5.update(chunk); size += len(chunk)
            temp.replace(path)
            local_md5 = md5.hexdigest(); sha256 = sha.hexdigest()
            last_modified = response.headers.get("last-modified")
        else:
            payload = path.read_bytes(); sha256 = hashlib.sha256(payload).hexdigest(); size = len(payload); last_modified = None
        yield {
            "source_id": source_id, "source_url": file_url, "format": format,
            "local_path": str(path), "size_bytes": size, "md5": local_md5,
            "remote_md5": remote_md5, "sha256": sha256, "changed": changed,
            "last_modified": last_modified,
        }

    return resource()
