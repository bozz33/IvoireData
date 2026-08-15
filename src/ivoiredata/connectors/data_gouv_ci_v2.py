from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import quote, unquote, urljoin, urlparse

from ..metadata import classify_from_base
from ..state_io import atomic_write_json
from ..streaming_snapshot import finalize_temp_snapshot, new_temp_path, stream_response_snapshot
from ..upstream_state import UpstreamState

PORTAL = "https://data.gouv.ci"
API = f"{PORTAL}/data-fair/api/v1"


def _safe_table(value: str) -> str:
    value = unquote(value).strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return ("datagouv_" + value)[:120]


def _dataset_id(meta: dict[str, Any]) -> str | None:
    for key in ("id", "slug", "name"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("results", "data", "items", "datasets", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _signature(meta: dict[str, Any]) -> str:
    keys = (
        "id", "slug", "updatedAt", "updated", "modified", "dataUpdatedAt",
        "finalizedAt", "size", "fileSize", "count", "lines", "version", "status",
        "schema", "isRest", "isVirtual",
    )
    compact = {key: meta.get(key) for key in keys}
    return hashlib.sha256(json.dumps(compact, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def _legacy_signature(meta: dict[str, Any]) -> str:
    compact = {key: meta.get(key) for key in ("id", "slug", "updatedAt", "updated", "modified", "size", "count", "version")}
    return hashlib.sha256(json.dumps(compact, sort_keys=True, default=str).encode()).hexdigest()


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", "replace")


def _rows(data: bytes) -> Iterable[dict[str, Any]]:
    """Compatibility parser retained for older tests/callers; new sync uses file streaming."""
    text = _decode(data)
    try:
        csv.field_size_limit(max(csv.field_size_limit(), 16 * 1024 * 1024))
    except OverflowError:
        pass
    try:
        dialect = csv.Sniffer().sniff(text[:65536], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    for row in csv.DictReader(io.StringIO(text), dialect=dialect):
        yield {str(k): v for k, v in row.items() if k is not None}


def _csv_settings(path: Path) -> tuple[str, Any]:
    try:
        csv.field_size_limit(max(csv.field_size_limit(), 16 * 1024 * 1024))
    except OverflowError:
        pass
    with path.open("rb") as handle:
        sample_bytes = handle.read(65536)
    encoding = "utf-8"
    sample = ""
    for candidate in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            sample = sample_bytes.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return encoding, dialect


def _iter_csv_path(path: Path) -> Iterator[dict[str, Any]]:
    encoding, dialect = _csv_settings(path)
    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, dialect=dialect)
        if path.stat().st_size > 0 and reader.fieldnames is None:
            raise RuntimeError("streamed /full payload has no CSV header")
        for row in reader:
            yield {str(k): v for k, v in row.items() if k is not None}


def _probe_csv(path: Path) -> None:
    encoding, dialect = _csv_settings(path)
    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, dialect=dialect)
        if path.stat().st_size > 0 and reader.fieldnames is None:
            raise RuntimeError("/full returned non-empty content without a CSV header")


def _iter_ndjson_path(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            item = json.loads(raw)
            if isinstance(item, dict):
                yield item


def _iter_legacy_lines_json(path: Path) -> Iterator[dict[str, Any]]:
    # Compatibility only for v0.8.1-v0.8.3 caches. New /lines snapshots are NDJSON.
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
    elif isinstance(payload, dict):
        for item in _items(payload):
            yield item


def dataset_id_from_public_url(url: str) -> str | None:
    path = urlparse(url).path.rstrip("/")
    marker = "/datasets/"
    return unquote(path.split(marker, 1)[1]).strip() or None if marker in path else None


def _classification(meta: dict[str, Any], dsid: str, metadata_base: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(str(meta.get(key) or "") for key in ("title", "name", "description", "keywords", "topics", "slug"))
    classified = classify_from_base(metadata_base, f"{PORTAL}/datasets/{dsid}", text, document_type="DATASET")
    return {
        "__ivoiredata_country_code": classified.get("country_code"),
        "__ivoiredata_country_name": classified.get("country_name"),
        "__ivoiredata_primary_domain": classified.get("primary_domain"),
        "__ivoiredata_secondary_domains_json": classified.get("secondary_domains_json"),
        "__ivoiredata_language": classified.get("language"),
        "__ivoiredata_document_type": "DATASET",
        "__ivoiredata_geographic_scope": classified.get("geographic_scope"),
        "__ivoiredata_classification_status": classified.get("classification_status"),
        "__ivoiredata_classification_confidence": classified.get("classification_confidence"),
    }


def _request_json(session, url: str, *, params: dict[str, Any] | None = None, timeout: int = 240):
    response = session.get(url, params=params, timeout=timeout, headers={"Accept": "application/json"})
    response.raise_for_status()
    return response, response.json()


def _discover_official(session, page_size: int = 1000) -> list[dict[str, Any]]:
    """List every anonymous-visible Data Fair dataset, even when server size is capped."""
    requested = max(1, min(int(page_size), 10000))
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 1
    while True:
        _, payload = _request_json(session, f"{API}/datasets", params={"size": requested, "page": page})
        batch = _items(payload)
        if not batch:
            break
        before = len(seen)
        for meta in batch:
            dsid = _dataset_id(meta)
            identity = dsid or hashlib.sha256(json.dumps(meta, sort_keys=True, default=str).encode()).hexdigest()
            if identity not in seen:
                seen.add(identity)
                collected.append(meta)
        count = payload.get("count") if isinstance(payload, dict) else None
        if isinstance(count, (int, float)):
            if len(collected) >= int(count):
                break
            if len(seen) == before:
                raise RuntimeError(
                    f"Data Fair /datasets pagination stalled at page={page}: "
                    f"collected={len(collected)} advertised_count={int(count)}"
                )
        elif len(batch) < requested:
            break
        page += 1
        if page > 10000:
            raise RuntimeError("Data Fair /datasets pagination exceeded safety limit")
    return collected


@dataclass
class _MaterializedRows:
    path: Path
    snapshot: dict[str, Any]
    source_url: str
    method: str
    row_format: str
    row_count_hint: int | None = None

    def rows(self) -> Iterator[dict[str, Any]]:
        if self.row_format == "csv":
            yield from _iter_csv_path(self.path)
        elif self.row_format == "ndjson":
            yield from _iter_ndjson_path(self.path)
        elif self.row_format == "legacy-json":
            yield from _iter_legacy_lines_json(self.path)
        else:
            raise ValueError(f"unknown Data.gouv row format: {self.row_format}")


def _snapshot_root(snapshot_dir: Path | None) -> Path:
    if snapshot_dir is not None:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        return snapshot_dir
    return Path(tempfile.mkdtemp(prefix="ivoiredata-datagouv-"))


def _discard_snapshot(snapshot: dict[str, Any]) -> None:
    value = str(snapshot.get("local_path") or "").strip()
    if not value:
        return
    path = Path(value)
    path.unlink(missing_ok=True)
    path.with_suffix(path.suffix + ".meta.json").unlink(missing_ok=True)


def _full_download_streaming(session, dsid: str, *, snapshot_dir: Path, source_id: str) -> _MaterializedRows:
    url = f"{API}/datasets/{quote(dsid, safe='')}/full"
    response = session.get(
        url,
        timeout=300,
        stream=True,
        headers={"Accept": "text/csv,application/csv,text/plain,*/*;q=0.5"},
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    snapshot = stream_response_snapshot(
        response,
        snapshot_dir,
        source_id=source_id,
        url=response.url,
        content_type=content_type or None,
        name=f"{dsid}.csv",
    )
    path = Path(str(snapshot["local_path"]))
    with path.open("rb") as handle:
        prefix = handle.read(1024).lstrip().lower()
    if ("text/html" in content_type and prefix.startswith(b"<")) or prefix.startswith(b"<!doctype html"):
        _discard_snapshot(snapshot)
        raise RuntimeError("/full returned HTML instead of dataset data")
    if prefix.startswith((b"{", b"[")) and "csv" not in content_type:
        _discard_snapshot(snapshot)
        raise RuntimeError("/full returned JSON-like content instead of CSV")
    try:
        _probe_csv(path)
    except Exception:
        _discard_snapshot(snapshot)
        raise
    return _MaterializedRows(
        path=path,
        snapshot=dict(snapshot),
        source_url=response.url,
        method="FULL_STREAM",
        row_format="csv",
    )


def _lines_download_streaming(
    session,
    dsid: str,
    *,
    snapshot_dir: Path,
    source_id: str,
    page_size: int = 10000,
) -> _MaterializedRows:
    """Follow Data Fair's official ``next`` URL and persist rows as streaming NDJSON."""
    base_url = f"{API}/datasets/{quote(dsid, safe='')}/lines"
    next_url: str | None = base_url
    params: dict[str, Any] | None = {
        "size": min(max(1, int(page_size)), 10000),
        "page": 1,
        "count": "exact",
    }
    visited: set[str] = set()
    requests_count = 0
    row_count = 0
    digest = hashlib.sha256()
    size_bytes = 0
    temp_path = new_temp_path(snapshot_dir, prefix=f"{dsid}-lines")
    try:
        with temp_path.open("wb") as handle:
            while next_url:
                request_key = next_url + ("?" + json.dumps(params, sort_keys=True) if params else "")
                if request_key in visited:
                    raise RuntimeError(f"Data Fair /lines pagination loop detected for {dsid}")
                visited.add(request_key)
                response, payload = _request_json(session, next_url, params=params, timeout=300)
                requests_count += 1
                for item in _items(payload):
                    encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
                    handle.write(encoded)
                    digest.update(encoded)
                    size_bytes += len(encoded)
                    row_count += 1
                raw_next = payload.get("next") if isinstance(payload, dict) else None
                if raw_next:
                    next_url = urljoin(response.url, str(raw_next))
                    params = None
                else:
                    next_url = None
                if requests_count > 100000:
                    raise RuntimeError(f"Data Fair /lines cursor exceeded safety limit for {dsid}")
            handle.flush()
        snapshot = finalize_temp_snapshot(
            snapshot_dir,
            temp_path=temp_path,
            source_id=source_id,
            url=base_url,
            content_type="application/x-ndjson",
            name=f"{dsid}-lines.jsonl",
            sha256=digest.hexdigest(),
            size_bytes=size_bytes,
        )
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return _MaterializedRows(
        path=Path(str(snapshot["local_path"])),
        snapshot=dict(snapshot),
        source_url=base_url,
        method="LINES_STREAM",
        row_format="ndjson",
        row_count_hint=row_count,
    )


def _cached_materialized(
    upstream: UpstreamState,
    source_id: str,
    artifact: str,
    signature: str,
) -> tuple[_MaterializedRows, dict[str, Any]] | None:
    state = upstream.get(source_id, artifact)
    path = upstream.cached_path(source_id, artifact, signature)
    if path is None:
        return None
    method = str(state.get("method") or "").upper()
    if method.startswith("FULL"):
        row_format = "csv"
    elif method == "LINES_STREAM":
        row_format = "ndjson"
    elif method.startswith("LINES"):
        row_format = "legacy-json"
    else:
        return None
    return (
        _MaterializedRows(
            path=path,
            snapshot={
                "sha256": state.get("sha256"),
                "size_bytes": state.get("size_bytes"),
                "local_path": str(path),
            },
            source_url=str(state.get("url") or f"{PORTAL}/datasets/{artifact.removeprefix('dataset:')}"),
            method=method,
            row_format=row_format,
            row_count_hint=int(state.get("rows")) if state.get("rows") is not None else None,
        ),
        state,
    )


def _has_physical_cache(
    upstream: UpstreamState | None,
    source_id: str,
    artifact: str,
    signature: str,
) -> bool:
    return bool(upstream and upstream.cached_path(source_id, artifact, signature) is not None)


def _archive_removed_tables(snapshot_dir: Path | None, dataset_ids: list[str]) -> list[dict[str, str]]:
    """Archive tables of datasets no longer visible publicly; never delete history."""
    if snapshot_dir is None or not dataset_ids:
        return []
    tables_root = snapshot_dir.parent / "tables" / "data"
    if not tables_root.exists():
        return []
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_root = snapshot_dir / "legacy" / "removed_upstream" / stamp
    archived: list[dict[str, str]] = []
    for dsid in sorted(set(dataset_ids)):
        table = _safe_table(dsid)
        source = tables_root / table
        if not source.exists():
            continue
        archive_root.mkdir(parents=True, exist_ok=True)
        target = archive_root / table
        suffix = 1
        while target.exists():
            target = archive_root / f"{table}-{suffix}"
            suffix += 1
        shutil.move(str(source), str(target))
        archived.append({"dataset_id": dsid, "table": table, "archive_path": str(target)})
    if archived:
        atomic_write_json(archive_root / "archive.json", {"archived_at": stamp, "datasets": archived})
    return archived


def data_gouv_ci_resource_v2(
    *,
    source_id: str = "civ_datagouv_catalog",
    dataset_ids: list[str] | None = None,
    limit: int | None = None,
    force: bool = False,
    user_agent: str = "IvoireData/0.8.4",
    snapshot_dir: Path | None = None,
    metadata_base: dict[str, Any] | None = None,
    upstream_state_path: Path | None = None,
):
    import dlt
    import requests

    base = dict(metadata_base or {})

    @dlt.resource(name="data_gouv_ci", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent})
        loaded_signatures = dlt.current.resource_state().setdefault("dataset_signatures", {})
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None
        raw_root = _snapshot_root(snapshot_dir)
        catalog = _discover_official(session)
        wanted = {str(value) for value in (dataset_ids or []) if value}
        selected: list[dict[str, Any]] = []
        catalog_ids: set[str] = set()
        classifications: dict[str, dict[str, Any]] = {}

        for meta in catalog:
            dsid = _dataset_id(meta)
            if not dsid:
                continue
            catalog_ids.add(dsid)
            identifiers = {dsid, str(meta.get("slug") or "")}
            if not wanted or identifiers & wanted:
                selected.append(meta)
            classified = _classification(meta, dsid, base)
            classifications[dsid] = classified
            row = dict(meta)
            row["__ivoiredata_source_url"] = f"{PORTAL}/datasets/{dsid}"
            row["__ivoiredata_dataset_signature"] = _signature(meta)
            row.update(classified)
            yield dlt.mark.with_table_name(row, "datagouv_catalog")

        if limit is not None:
            selected = selected[: max(0, int(limit))]

        stats: dict[str, Any] = {
            "catalog_visible_anonymous": len(catalog_ids),
            "selected": len(selected),
            "unchanged": 0,
            "backfill_missing_raw": 0,
            "physically_backfilled": 0,
            "reappeared": 0,
            "replayed_from_local_cache": 0,
            "downloaded": 0,
            "via_full_stream": 0,
            "via_lines_stream": 0,
            "empty": 0,
            "failed": 0,
            "removed_upstream": 0,
            "removed_upstream_ids": [],
            "archived_removed_tables": [],
            "business_rows_changed": 0,
            "failures": [],
            "pagination": "Data Fair page>=1 until advertised count is reached; /lines follows next cursor until absent",
            "streaming": True,
            "force_semantics": "force checks now; unchanged versions with verified local snapshots are not downloaded again",
            "physical_truth": "DLT signature alone never proves a fetched artifact; missing raw snapshots are backfilled",
        }

        if upstream:
            previous_rows = upstream.source_rows(source_id)
            previous = {
                str(row.get("artifact_id", ""))[8:]
                for row in previous_rows
                if str(row.get("artifact_id", "")).startswith("dataset:") and row.get("downloaded")
            }
            removed_ids = sorted(previous - catalog_ids)
            for removed in removed_ids:
                upstream.mark_removed(source_id, f"dataset:{removed}")
            stats["removed_upstream"] = len(removed_ids)
            stats["removed_upstream_ids"] = removed_ids
            stats["archived_removed_tables"] = _archive_removed_tables(snapshot_dir, removed_ids)

        for meta in selected:
            dsid = _dataset_id(meta)
            assert dsid is not None
            signature = _signature(meta)
            legacy_signature = _legacy_signature(meta)
            artifact = f"dataset:{dsid}"
            cached_state = upstream.get(source_id, artifact) if upstream else {}
            if cached_state.get("removed"):
                loaded_signatures.pop(dsid, None)
                stats["reappeared"] += 1
            loaded = loaded_signatures.get(dsid)

            physical_current = _has_physical_cache(upstream, source_id, artifact, signature)
            if loaded == signature and physical_current:
                stats["unchanged"] += 1
                assert upstream is not None
                upstream.mark_unchanged(
                    source_id,
                    artifact,
                    signature=signature,
                    url=f"{PORTAL}/datasets/{dsid}",
                    reason="SIGNATURE_AND_LOCAL_SNAPSHOT",
                )
                continue

            historical_without_raw = loaded in {signature, legacy_signature} and not physical_current
            if historical_without_raw:
                # Pre-v0.8.4 DLT state proves that rows were processed, not that the raw
                # upstream bytes still exist. Fetch once to establish physical truth.
                stats["backfill_missing_raw"] += 1

            materialized: _MaterializedRows | None = None
            replay = _cached_materialized(upstream, source_id, artifact, signature) if upstream else None
            if replay is not None:
                materialized, _ = replay
                stats["replayed_from_local_cache"] += 1
            else:
                full_error: Exception | None = None
                full_status: int | None = None
                try:
                    materialized = _full_download_streaming(
                        session,
                        dsid,
                        snapshot_dir=raw_root,
                        source_id=source_id,
                    )
                    stats["via_full_stream"] += 1
                except Exception as exc:
                    full_error = exc
                    full_status = getattr(getattr(exc, "response", None), "status_code", None)
                    try:
                        materialized = _lines_download_streaming(
                            session,
                            dsid,
                            snapshot_dir=raw_root,
                            source_id=source_id,
                        )
                        stats["via_lines_stream"] += 1
                    except Exception as lines_exc:
                        lines_status = getattr(getattr(lines_exc, "response", None), "status_code", None)
                        error = f"/full: {full_error}; /lines: {lines_exc}"
                        stats["failed"] += 1
                        stats["failures"].append({
                            "dataset_id": dsid,
                            "full_status": full_status,
                            "lines_status": lines_status,
                            "error": error[:1000],
                        })
                        if upstream:
                            upstream.mark_error(
                                source_id,
                                artifact,
                                url=f"{API}/datasets/{quote(dsid, safe='')}/full",
                                error=error,
                                status_code=lines_status,
                                method="FULL_STREAM+LINES_STREAM",
                            )
                        continue
                stats["downloaded"] += 1
                if historical_without_raw:
                    stats["physically_backfilled"] += 1

            assert materialized is not None
            snapshot = materialized.snapshot
            raw_sha = str(snapshot.get("sha256") or "") or None
            raw_path = str(snapshot.get("local_path") or "") or None
            raw_size = int(snapshot.get("size_bytes") or 0)
            if upstream:
                # Persist the local body before yielding rows. If dlt crashes later, the
                # next run can replay this exact snapshot without another network body.
                upstream.mark_downloaded(
                    source_id,
                    artifact,
                    url=materialized.source_url,
                    signature=signature,
                    sha256=raw_sha,
                    size_bytes=raw_size,
                    method=materialized.method,
                    rows=materialized.row_count_hint,
                    local_path=raw_path,
                    extra={"streaming": True, "body_changed": replay is None},
                )

            classified = classifications.get(dsid) or _classification(meta, dsid, base)
            table = _safe_table(dsid)
            row_count = 0
            for index, source_row in enumerate(materialized.rows()):
                row_count += 1
                item = dict(source_row)
                item.update({
                    "__ivoiredata_dataset_id": dsid,
                    "__ivoiredata_source_url": f"{PORTAL}/datasets/{dsid}",
                    "__ivoiredata_row_index": index,
                    "__ivoiredata_raw_sha256": raw_sha,
                    "__ivoiredata_raw_path": raw_path,
                    "__ivoiredata_fetch_method": materialized.method,
                    **classified,
                })
                yield dlt.mark.with_table_name(item, table)

            if row_count == 0:
                stats["empty"] += 1
            loaded_signatures[dsid] = signature
            stats["business_rows_changed"] += row_count
            if upstream:
                upstream.mark_downloaded(
                    source_id,
                    artifact,
                    url=materialized.source_url,
                    signature=signature,
                    sha256=raw_sha,
                    size_bytes=raw_size,
                    method=materialized.method,
                    rows=row_count,
                    local_path=raw_path,
                    extra={"streaming": True, "body_changed": replay is None},
                )

        if snapshot_dir:
            atomic_write_json(snapshot_dir / "datagouv_sync_stats.json", stats)
        yield dlt.mark.with_table_name(
            {"run_stats_json": json.dumps(stats, ensure_ascii=False), **stats},
            "datagouv_sync_stats",
        )

    return resource()


data_gouv_ci_resource = data_gouv_ci_resource_v2
