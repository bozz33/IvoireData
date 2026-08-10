from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlparse

from ..metadata import classify_from_base
from ..snapshots import save_snapshot
from ..state_io import atomic_write_json
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
    # Data Fair exposes update metadata and row/size information. The complete subset
    # below deliberately changes when either data or schema-relevant metadata changes.
    keys = (
        "id", "slug", "updatedAt", "updated", "modified", "dataUpdatedAt",
        "finalizedAt", "size", "fileSize", "count", "lines", "version", "status",
        "schema", "isRest", "isVirtual",
    )
    compact = {k: meta.get(k) for k in keys}
    return hashlib.sha256(json.dumps(compact, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", "replace")


def _rows(data: bytes) -> Iterable[dict[str, Any]]:
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


def _request_json(session, url: str, timeout: int = 120, *, params: dict[str, Any] | None = None) -> tuple[Any, Any]:
    r = session.get(url, params=params, timeout=timeout, headers={"Accept": "application/json"})
    r.raise_for_status()
    return r, r.json()


def _discover(session, page_size: int = 1000) -> list[dict[str, Any]]:
    """Discover datasets visible to an anonymous Data Fair user.

    Data Fair's public catalogue is permission-aware: anonymous callers only see
    opendata datasets. We page defensively even though data.gouv.ci is currently
    small enough for a single 1000-item response.
    """
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 1
    while True:
        payload = None
        for params in ({"size": page_size, "page": page}, {"size": page_size, "page": page - 1}):
            try:
                _, candidate = _request_json(session, f"{API}/datasets", params=params)
                batch = _items(candidate)
            except Exception:
                continue
            if batch or page == 1:
                payload = candidate
                break
        if payload is None:
            if collected:
                break
            raise RuntimeError("data.gouv.ci catalog API did not return datasets")
        batch = _items(payload)
        new_count = 0
        for meta in batch:
            dsid = _dataset_id(meta)
            identity = dsid or hashlib.sha256(json.dumps(meta, sort_keys=True, default=str).encode()).hexdigest()
            if identity in seen:
                continue
            seen.add(identity)
            collected.append(meta)
            new_count += 1
        total = payload.get("total") if isinstance(payload, dict) else None
        if not batch or new_count == 0 or len(batch) < page_size:
            break
        if isinstance(total, int) and len(collected) >= total:
            break
        page += 1
        if page > 100:
            raise RuntimeError("data.gouv.ci catalog pagination exceeded safety limit")
    return collected


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


def _full_download(session, dsid: str):
    url = f"{API}/datasets/{quote(dsid, safe='')}/full"
    response = session.get(url, timeout=240, headers={"Accept": "text/csv,application/csv,text/plain,*/*;q=0.5"})
    response.raise_for_status()
    ctype = response.headers.get("content-type", "").lower()
    if "text/html" in ctype and response.content.lstrip().startswith(b"<"):
        raise RuntimeError("/full returned HTML instead of dataset data")
    parsed = list(_rows(response.content))
    if not parsed and response.content.strip():
        raise RuntimeError("/full returned non-empty content that could not be parsed as tabular CSV")
    return response, parsed


def _lines_download(session, dsid: str, *, snapshot_dir: Path | None, page_size: int = 1000) -> tuple[list[dict[str, Any]], list[dict[str, object]], str]:
    """Use Data Fair's documented per-dataset /lines API with pagination."""
    url = f"{API}/datasets/{quote(dsid, safe='')}/lines"
    page = 1
    rows: list[dict[str, Any]] = []
    snapshots: list[dict[str, object]] = []
    seen_first_ids: set[str] = set()
    while True:
        response, payload = _request_json(session, url, timeout=240, params={"size": page_size, "page": page})
        batch = _items(payload)
        if not batch and page == 1:
            # Some Data Fair deployments use zero-based pages.
            response, payload = _request_json(session, url, timeout=240, params={"size": page_size, "page": 0})
            batch = _items(payload)
        if batch:
            first = str(batch[0].get("_id") or batch[0].get("id") or "")
            if first and first in seen_first_ids:
                break
            if first:
                seen_first_ids.add(first)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        snapshots.append(save_snapshot(
            snapshot_dir,
            source_id="civ_datagouv_catalog",
            url=response.url,
            content=raw,
            content_type="application/json",
            name=f"{dsid}-lines-page-{page:05d}.json",
        ))
        rows.extend(batch)
        total = payload.get("total") if isinstance(payload, dict) else None
        if not batch or len(batch) < page_size:
            break
        if isinstance(total, int) and len(rows) >= total:
            break
        page += 1
        if page > 100000:
            raise RuntimeError(f"/lines pagination exceeded safety limit for {dsid}")
    return rows, snapshots, url


def data_gouv_ci_resource(
    *,
    dataset_ids: list[str] | None = None,
    limit: int | None = None,
    force: bool = False,
    user_agent: str = "IvoireData/0.8.2",
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
        legacy_state = dlt.current.resource_state().setdefault("dataset_signatures", {})
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None
        catalog = _discover(session)
        wanted = set(dataset_ids or [])
        selected: list[dict[str, Any]] = []
        catalog_ids: set[str] = set()
        for meta in catalog:
            dsid = _dataset_id(meta)
            if not dsid:
                continue
            catalog_ids.add(dsid)
            identifiers = {dsid, meta.get("slug")}
            if not wanted or identifiers & wanted:
                selected.append(meta)
        if limit is not None:
            selected = selected[:limit]

        classifications: dict[str, dict[str, Any]] = {}
        for meta in catalog:
            dsid = _dataset_id(meta)
            if dsid:
                classified = _classification(meta, dsid, base)
                classifications[dsid] = classified
                row = dict(meta)
                row["__ivoiredata_source_url"] = f"{PORTAL}/datasets/{dsid}"
                row["__ivoiredata_dataset_signature"] = _signature(meta)
                row.update(classified)
                yield dlt.mark.with_table_name(row, "datagouv_catalog")

        stats: dict[str, Any] = {
            "catalog_visible_anonymous": len(catalog_ids),
            "selected": len(selected),
            "unchanged": 0,
            "downloaded": 0,
            "via_full": 0,
            "via_lines": 0,
            "empty": 0,
            "failed": 0,
            "removed_upstream": 0,
            "business_rows": 0,
            "failures": [],
            "force_semantics": "force rechecks catalogue; unchanged dataset signatures are never re-downloaded",
        }

        if upstream:
            previous = {
                str(row.get("artifact_id", ""))[8:]
                for row in upstream.source_rows("civ_datagouv_catalog")
                if str(row.get("artifact_id", "")).startswith("dataset:") and row.get("downloaded")
            }
            for removed in sorted(previous - catalog_ids):
                upstream.mark_removed("civ_datagouv_catalog", f"dataset:{removed}")
                stats["removed_upstream"] += 1

        for meta in selected:
            dsid = _dataset_id(meta)
            assert dsid is not None
            sig = _signature(meta)
            artifact = f"dataset:{dsid}"
            # Preserve the old dlt state during migration so the first v0.8.2 run does
            # not re-download datasets that v0.8.1 already proved unchanged.
            if (upstream and upstream.signature_matches("civ_datagouv_catalog", artifact, sig)) or legacy_state.get(dsid) == sig:
                if upstream and not upstream.signature_matches("civ_datagouv_catalog", artifact, sig):
                    upstream.mark_downloaded(
                        "civ_datagouv_catalog", artifact,
                        url=f"{PORTAL}/datasets/{dsid}", signature=sig,
                        sha256=None, size_bytes=None, method="ADOPTED_V081_STATE",
                    )
                elif upstream:
                    upstream.mark_unchanged("civ_datagouv_catalog", artifact, signature=sig, url=f"{PORTAL}/datasets/{dsid}")
                stats["unchanged"] += 1
                continue

            rows: list[dict[str, Any]] = []
            method = "FULL"
            source_url = f"{API}/datasets/{quote(dsid, safe='')}/full"
            snapshot: dict[str, object] | None = None
            full_error: Exception | None = None
            full_status: int | None = None
            try:
                response, rows = _full_download(session, dsid)
                snapshot = save_snapshot(
                    snapshot_dir,
                    source_id="civ_datagouv_catalog",
                    url=response.url,
                    content=response.content,
                    content_type=response.headers.get("content-type"),
                    name=f"{dsid}.csv",
                )
                source_url = response.url
                stats["via_full"] += 1
            except Exception as exc:
                full_error = exc
                full_status = getattr(getattr(exc, "response", None), "status_code", None)
                method = "LINES"
                try:
                    rows, line_snapshots, lines_url = _lines_download(session, dsid, snapshot_dir=snapshot_dir)
                    source_url = lines_url
                    if line_snapshots:
                        snapshot = line_snapshots[0]
                    stats["via_lines"] += 1
                except Exception as lines_exc:
                    status = getattr(getattr(lines_exc, "response", None), "status_code", None)
                    error = f"/full: {full_error}; /lines: {lines_exc}"
                    stats["failed"] += 1
                    stats["failures"].append({"dataset_id": dsid, "full_status": full_status, "lines_status": status, "error": error[:1000]})
                    if upstream:
                        upstream.mark_error("civ_datagouv_catalog", artifact, url=source_url, error=error, status_code=status, method="FULL+LINES")
                    print(f"[data_gouv_ci] dataset non récupérable {dsid} -> {error}", flush=True)
                    continue

            if not rows:
                stats["empty"] += 1
            classified = classifications.get(dsid) or _classification(meta, dsid, base)
            raw_sha = str(snapshot.get("sha256")) if snapshot else None
            raw_path = snapshot.get("local_path") if snapshot else None
            table = _safe_table(dsid)
            for idx, row in enumerate(rows):
                item = dict(row)
                item.update({
                    "__ivoiredata_dataset_id": dsid,
                    "__ivoiredata_source_url": f"{PORTAL}/datasets/{dsid}",
                    "__ivoiredata_row_index": idx,
                    "__ivoiredata_raw_sha256": raw_sha,
                    "__ivoiredata_raw_path": raw_path,
                    "__ivoiredata_fetch_method": method,
                    **classified,
                })
                yield dlt.mark.with_table_name(item, table)
            legacy_state[dsid] = sig
            stats["downloaded"] += 1
            stats["business_rows"] += len(rows)
            if upstream:
                upstream.mark_downloaded(
                    "civ_datagouv_catalog", artifact,
                    url=source_url,
                    signature=sig,
                    sha256=raw_sha,
                    size_bytes=int(snapshot.get("size_bytes") or 0) if snapshot else None,
                    method=method,
                    rows=len(rows),
                )

        if snapshot_dir:
            atomic_write_json(snapshot_dir / "datagouv_sync_stats.json", stats)
        yield dlt.mark.with_table_name({"run_stats_json": json.dumps(stats, ensure_ascii=False), **stats}, "datagouv_sync_stats")

    return resource()
