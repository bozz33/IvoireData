from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urljoin, urlparse

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
    """List anonymous-visible datasets using Data Fair's documented page>=1 contract."""
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 1
    while True:
        _, payload = _request_json(session, f"{API}/datasets", params={"size": page_size, "page": page})
        batch = _items(payload)
        for meta in batch:
            dsid = _dataset_id(meta)
            identity = dsid or hashlib.sha256(json.dumps(meta, sort_keys=True, default=str).encode()).hexdigest()
            if identity not in seen:
                seen.add(identity)
                collected.append(meta)
        count = payload.get("count") if isinstance(payload, dict) else None
        if not batch or len(batch) < page_size:
            break
        if isinstance(count, (int, float)) and len(collected) >= int(count):
            break
        page += 1
        if page > 10000:
            raise RuntimeError("Data Fair /datasets pagination exceeded safety limit")
    return collected


def _full_download(session, dsid: str):
    url = f"{API}/datasets/{quote(dsid, safe='')}/full"
    response = session.get(url, timeout=240, headers={"Accept": "text/csv,application/csv,text/plain,*/*;q=0.5"})
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" in content_type and response.content.lstrip().startswith(b"<"):
        raise RuntimeError("/full returned HTML instead of dataset data")
    parsed = list(_rows(response.content))
    if not parsed and response.content.strip():
        raise RuntimeError("/full returned non-empty content that could not be parsed as tabular CSV")
    return response, parsed


def _lines_download_official(session, dsid: str, *, snapshot_dir: Path | None, source_id: str,
                             page_size: int = 10000) -> tuple[list[dict[str, Any]], dict[str, object] | None, str]:
    """Read all lines by following Data Fair's official `next` cursor URL."""
    base_url = f"{API}/datasets/{quote(dsid, safe='')}/lines"
    next_url: str | None = base_url
    params: dict[str, Any] | None = {"size": min(max(1, int(page_size)), 10000), "page": 1, "count": "exact"}
    rows: list[dict[str, Any]] = []
    visited: set[str] = set()
    requests_count = 0
    while next_url:
        request_key = next_url + ("?" + json.dumps(params, sort_keys=True) if params else "")
        if request_key in visited:
            raise RuntimeError(f"Data Fair /lines pagination loop detected for {dsid}")
        visited.add(request_key)
        response, payload = _request_json(session, next_url, params=params, timeout=300)
        requests_count += 1
        rows.extend(_items(payload))
        raw_next = payload.get("next") if isinstance(payload, dict) else None
        if raw_next:
            next_url = urljoin(response.url, str(raw_next))
            params = None
        else:
            next_url = None
        if requests_count > 100000:
            raise RuntimeError(f"Data Fair /lines cursor exceeded safety limit for {dsid}")

    canonical = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    snapshot = save_snapshot(
        snapshot_dir,
        source_id=source_id,
        url=base_url,
        content=canonical,
        content_type="application/json",
        name=f"{dsid}-lines.json",
    ) if snapshot_dir is not None else None
    return rows, snapshot, base_url


def _cached_rows(upstream: UpstreamState, source_id: str, artifact: str, signature: str) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    state = upstream.get(source_id, artifact)
    path = upstream.cached_path(source_id, artifact, signature)
    if path is None:
        return None
    method = str(state.get("method") or "").upper()
    try:
        if method == "FULL":
            rows = list(_rows(path.read_bytes()))
        elif method == "LINES":
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = _items(payload) if isinstance(payload, dict) else [item for item in payload if isinstance(item, dict)]
        else:
            return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return rows, state


def data_gouv_ci_resource_v2(
    *,
    source_id: str = "civ_datagouv_catalog",
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
        loaded_signatures = dlt.current.resource_state().setdefault("dataset_signatures", {})
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None
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
            "adopted_v081_signature": 0,
            "replayed_from_local_cache": 0,
            "downloaded": 0,
            "via_full": 0,
            "via_lines": 0,
            "empty": 0,
            "failed": 0,
            "removed_upstream": 0,
            "business_rows_changed": 0,
            "failures": [],
            "pagination": "Data Fair page>=1 for catalog; follow /lines next cursor until absent",
            "force_semantics": "force checks now; unchanged dataset versions are not downloaded again",
        }

        if upstream:
            previous = {
                str(row.get("artifact_id", ""))[8:]
                for row in upstream.source_rows(source_id)
                if str(row.get("artifact_id", "")).startswith("dataset:") and row.get("downloaded")
            }
            for removed in sorted(previous - catalog_ids):
                upstream.mark_removed(source_id, f"dataset:{removed}")
                stats["removed_upstream"] += 1

        for meta in selected:
            dsid = _dataset_id(meta)
            assert dsid is not None
            signature = _signature(meta)
            loaded = loaded_signatures.get(dsid)
            artifact = f"dataset:{dsid}"

            if loaded == signature:
                stats["unchanged"] += 1
                if upstream:
                    upstream.mark_unchanged(source_id, artifact, signature=signature, url=f"{PORTAL}/datasets/{dsid}")
                continue

            if loaded and loaded == _legacy_signature(meta):
                loaded_signatures[dsid] = signature
                stats["adopted_v081_signature"] += 1
                if upstream:
                    upstream.mark_downloaded(
                        source_id, artifact, url=f"{PORTAL}/datasets/{dsid}", signature=signature,
                        sha256=None, size_bytes=None, method="ADOPTED_V081_SIGNATURE",
                    )
                continue

            rows: list[dict[str, Any]]
            method = "FULL"
            source_url = f"{API}/datasets/{quote(dsid, safe='')}/full"
            snapshot: dict[str, Any] | None = None

            replay = _cached_rows(upstream, source_id, artifact, signature) if upstream else None
            if replay is not None:
                rows, cache = replay
                method = str(cache.get("method") or "FULL").upper()
                source_url = str(cache.get("url") or source_url)
                cached_path = upstream.cached_path(source_id, artifact, signature) if upstream else None
                snapshot = {
                    "sha256": cache.get("sha256"),
                    "size_bytes": cache.get("size_bytes"),
                    "local_path": str(cached_path) if cached_path else None,
                }
                stats["replayed_from_local_cache"] += 1
            else:
                full_error: Exception | None = None
                full_status: int | None = None
                try:
                    response, rows = _full_download(session, dsid)
                    snapshot = save_snapshot(
                        snapshot_dir, source_id=source_id, url=response.url,
                        content=response.content, content_type=response.headers.get("content-type"),
                        name=f"{dsid}.csv",
                    )
                    source_url = response.url
                    stats["via_full"] += 1
                except Exception as exc:
                    full_error = exc
                    full_status = getattr(getattr(exc, "response", None), "status_code", None)
                    method = "LINES"
                    try:
                        rows, snapshot, source_url = _lines_download_official(
                            session, dsid, snapshot_dir=snapshot_dir, source_id=source_id
                        )
                        stats["via_lines"] += 1
                    except Exception as lines_exc:
                        lines_status = getattr(getattr(lines_exc, "response", None), "status_code", None)
                        error = f"/full: {full_error}; /lines: {lines_exc}"
                        stats["failed"] += 1
                        stats["failures"].append({
                            "dataset_id": dsid, "full_status": full_status,
                            "lines_status": lines_status, "error": error[:1000],
                        })
                        if upstream:
                            upstream.mark_error(
                                source_id, artifact, url=source_url, error=error,
                                status_code=lines_status, method="FULL+LINES",
                            )
                        continue

                stats["downloaded"] += 1
                if upstream:
                    upstream.mark_downloaded(
                        source_id, artifact, url=source_url, signature=signature,
                        sha256=str(snapshot.get("sha256")) if snapshot else None,
                        size_bytes=int(snapshot.get("size_bytes") or 0) if snapshot else None,
                        method=method, rows=len(rows),
                        local_path=(str(snapshot.get("local_path") or "") or None) if snapshot else None,
                    )

            if not rows:
                stats["empty"] += 1
            classified = classifications.get(dsid) or _classification(meta, dsid, base)
            raw_sha = str(snapshot.get("sha256")) if snapshot and snapshot.get("sha256") else None
            raw_path = snapshot.get("local_path") if snapshot else None
            table = _safe_table(dsid)
            for index, source_row in enumerate(rows):
                item = dict(source_row)
                item.update({
                    "__ivoiredata_dataset_id": dsid,
                    "__ivoiredata_source_url": f"{PORTAL}/datasets/{dsid}",
                    "__ivoiredata_row_index": index,
                    "__ivoiredata_raw_sha256": raw_sha,
                    "__ivoiredata_raw_path": raw_path,
                    "__ivoiredata_fetch_method": method,
                    **classified,
                })
                yield dlt.mark.with_table_name(item, table)
            loaded_signatures[dsid] = signature
            stats["business_rows_changed"] += len(rows)

        if snapshot_dir:
            atomic_write_json(snapshot_dir / "datagouv_sync_stats.json", stats)
        yield dlt.mark.with_table_name(
            {"run_stats_json": json.dumps(stats, ensure_ascii=False), **stats},
            "datagouv_sync_stats",
        )

    return resource()


data_gouv_ci_resource = data_gouv_ci_resource_v2
