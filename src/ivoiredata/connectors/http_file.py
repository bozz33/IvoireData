from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from ..snapshots import save_snapshot
from ..upstream_state import UpstreamState


def _format(url: str, content_type: str) -> str:
    suffix = PurePosixPath(urlparse(url).path).suffix.lower().lstrip(".")
    if suffix in {"csv", "json", "jsonl", "xlsx", "xls", "parquet"}:
        return suffix
    c = content_type.lower()
    if "csv" in c:
        return "csv"
    if "json" in c:
        return "json"
    if "spreadsheet" in c or "excel" in c:
        return "xlsx"
    if "parquet" in c:
        return "parquet"
    return suffix or "binary"


def http_file_resource(
    *,
    source_id: str,
    url: str,
    user_agent: str = "IvoireData/0.8.2",
    force: bool = False,
    snapshot_dir: Path | None = None,
    upstream_state_path: Path | None = None,
):
    import dlt
    import requests

    @dlt.resource(name="structured_files", write_disposition="replace")
    def resource():
        state = dlt.current.resource_state().setdefault("content_hashes", {})
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None
        artifact = "file"
        headers = {"User-Agent": user_agent}
        if upstream:
            headers.update(upstream.conditional_headers(source_id, artifact))
        r = requests.get(url, timeout=180, headers=headers)
        if r.status_code == 304 and state.get(url):
            if upstream:
                upstream.mark_http_unchanged(source_id, artifact, url=url)
            return
        r.raise_for_status()
        digest = hashlib.sha256(r.content).hexdigest()
        # `force` only bypasses the freshness scheduler. It does not duplicate an
        # identical immutable file version.
        if state.get(url) == digest:
            if upstream:
                upstream.mark_unchanged(
                    source_id, artifact, signature=digest, url=r.url,
                    etag=r.headers.get("etag"), last_modified=r.headers.get("last-modified"), reason="SHA256",
                )
            return
        fmt = _format(url, r.headers.get("content-type", ""))
        snapshot = save_snapshot(snapshot_dir, source_id=source_id, url=url, content=r.content, content_type=r.headers.get("content-type"))

        def enrich(row, idx):
            item = dict(row)
            item.update({
                "__ivoiredata_source_id": source_id,
                "__ivoiredata_source_url": url,
                "__ivoiredata_format": fmt,
                "__ivoiredata_raw_sha256": digest,
                "__ivoiredata_raw_path": snapshot.get("local_path"),
                "__ivoiredata_row_index": idx,
            })
            return item

        rows = []
        emitted = 0
        if fmt == "csv":
            text = r.content.decode("utf-8-sig", "replace")
            try:
                dialect = csv.Sniffer().sniff(text[:65536], delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
        elif fmt in {"json", "jsonl"}:
            text = r.content.decode("utf-8", "replace")
            if fmt == "jsonl":
                rows = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                payload = json.loads(text)
                if isinstance(payload, list):
                    rows = payload
                elif isinstance(payload, dict):
                    for key in ("results", "data", "items", "records"):
                        if isinstance(payload.get(key), list):
                            rows = payload[key]
                            break
                    else:
                        rows = [payload]
        elif fmt in {"xlsx", "xls", "parquet"}:
            try:
                import pandas as pd
            except ImportError as exc:
                raise RuntimeError("pandas is required for spreadsheet/parquet sources") from exc
            bio = io.BytesIO(r.content)
            if fmt in {"xlsx", "xls"}:
                frames = pd.read_excel(bio, sheet_name=None)
                for sheet, frame in frames.items():
                    for idx, row in enumerate(frame.where(frame.notna(), None).to_dict(orient="records")):
                        item = enrich(row, idx)
                        item["__ivoiredata_sheet"] = str(sheet)
                        emitted += 1
                        yield dlt.mark.with_table_name(item, f"file_{source_id}")
                state[url] = digest
                if upstream:
                    upstream.mark_downloaded(
                        source_id, artifact, url=r.url, signature=digest,
                        sha256=digest, size_bytes=len(r.content), etag=r.headers.get("etag"),
                        last_modified=r.headers.get("last-modified"), method="HTTP_VALIDATORS+SHA256",
                        rows=emitted, local_path=str(snapshot.get("local_path") or "") or None,
                    )
                return
            frame = pd.read_parquet(bio)
            rows = frame.where(frame.notna(), None).to_dict(orient="records")
        else:
            raise RuntimeError(f"unsupported structured file format: {fmt}")

        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                row = {"value": row}
            emitted += 1
            yield dlt.mark.with_table_name(enrich(row, idx), f"file_{source_id}")
        state[url] = digest
        if upstream:
            upstream.mark_downloaded(
                source_id, artifact, url=r.url, signature=digest,
                sha256=digest, size_bytes=len(r.content), etag=r.headers.get("etag"),
                last_modified=r.headers.get("last-modified"), method="HTTP_VALIDATORS+SHA256",
                rows=emitted, local_path=str(snapshot.get("local_path") or "") or None,
            )

    return resource()
