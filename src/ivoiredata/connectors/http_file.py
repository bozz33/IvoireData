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


def _valid_cached_bytes(path: Path | None, cached: dict) -> tuple[bytes, str] | None:
    """Return an integrity-checked cached payload, if one is available."""
    if path is None:
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    expected = str(cached.get("sha256") or cached.get("signature") or "").strip()
    if expected and digest != expected:
        return None
    return raw, digest


def http_file_resource(
    *,
    source_id: str,
    url: str,
    user_agent: str = "IvoireData/0.8.3",
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
        committed_digest = state.get(url)
        cached = upstream.get(source_id, artifact) if upstream else {}
        cached_path = upstream.cached_path(source_id, artifact) if upstream else None
        cached_payload = _valid_cached_bytes(cached_path, cached)

        headers = {"User-Agent": user_agent}
        # Validators are safe if the matching body was already committed, or if we have
        # an integrity-checked local body that can be replayed after a 304. This avoids
        # both the false-304 crash hole and an unnecessary second body transfer.
        if upstream and (
            (committed_digest and cached.get("signature") == committed_digest)
            or cached_payload is not None
        ):
            headers.update(upstream.conditional_headers(source_id, artifact))

        response = requests.get(url, timeout=180, headers=headers)
        replayed_from_cache = False
        raw: bytes
        digest: str
        content_type: str
        response_url: str
        response_headers: dict

        if response.status_code == 304:
            if committed_digest and cached.get("signature") == committed_digest:
                if upstream:
                    upstream.mark_http_unchanged(
                        source_id, artifact, url=url,
                        extra={"signature": committed_digest, "local_path": str(cached_path) if cached_path else None},
                    )
                return
            if cached_payload is None:
                # A validator without a replayable/committed body cannot prove local
                # materialization. Retry once without conditionals and recover normally.
                response = requests.get(url, timeout=180, headers={"User-Agent": user_agent})
                response.raise_for_status()
            else:
                raw, digest = cached_payload
                replayed_from_cache = True
                content_type = str(cached.get("content_type") or "")
                response_url = str(cached.get("url") or url)
                response_headers = {
                    "etag": cached.get("etag"),
                    "last-modified": cached.get("last_modified"),
                }

        if not replayed_from_cache:
            response.raise_for_status()
            raw = response.content
            digest = hashlib.sha256(raw).hexdigest()
            content_type = response.headers.get("content-type", "")
            response_url = response.url
            response_headers = dict(response.headers)

        # `force` only bypasses the freshness scheduler. It never duplicates an already
        # committed immutable file version.
        if committed_digest == digest:
            if upstream:
                upstream.mark_unchanged(
                    source_id, artifact, signature=digest, url=response_url,
                    etag=response_headers.get("etag"), last_modified=response_headers.get("last-modified"),
                    reason="SHA256", extra={"content_type": content_type or cached.get("content_type")},
                )
            return

        format_hint_url = str(cached_path) if replayed_from_cache and cached_path else url
        fmt = _format(format_hint_url, content_type)
        if replayed_from_cache and cached_path is not None:
            snapshot = {
                "sha256": digest,
                "size_bytes": len(raw),
                "source_url": response_url,
                "local_path": str(cached_path),
            }
        else:
            snapshot = save_snapshot(
                snapshot_dir,
                source_id=source_id,
                url=url,
                content=raw,
                content_type=content_type,
            )

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
            text = raw.decode("utf-8-sig", "replace")
            try:
                dialect = csv.Sniffer().sniff(text[:65536], delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
        elif fmt in {"json", "jsonl"}:
            text = raw.decode("utf-8", "replace")
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
            bio = io.BytesIO(raw)
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
                        source_id, artifact, url=response_url, signature=digest,
                        sha256=digest, size_bytes=len(raw), etag=response_headers.get("etag"),
                        last_modified=response_headers.get("last-modified"),
                        method="CACHE_REPLAY_AFTER_304" if replayed_from_cache else "HTTP_VALIDATORS+SHA256",
                        rows=emitted, local_path=str(snapshot.get("local_path") or "") or None,
                        extra={"content_type": content_type or cached.get("content_type")},
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
                source_id, artifact, url=response_url, signature=digest,
                sha256=digest, size_bytes=len(raw), etag=response_headers.get("etag"),
                last_modified=response_headers.get("last-modified"),
                method="CACHE_REPLAY_AFTER_304" if replayed_from_cache else "HTTP_VALIDATORS+SHA256",
                rows=emitted, local_path=str(snapshot.get("local_path") or "") or None,
                extra={"content_type": content_type or cached.get("content_type")},
            )

    return resource()