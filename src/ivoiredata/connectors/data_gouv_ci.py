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
        for key in ("results", "data", "items", "datasets"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _signature(meta: dict[str, Any]) -> str:
    compact = {k: meta.get(k) for k in ("id", "slug", "updatedAt", "updated", "modified", "size", "count", "version")}
    return hashlib.sha256(json.dumps(compact, sort_keys=True, default=str).encode()).hexdigest()


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


def _request_json(session, url: str, timeout: int = 120) -> Any:
    r = session.get(url, timeout=timeout, headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def _discover(session, page_size: int = 1000) -> list[dict[str, Any]]:
    for url in (f"{API}/datasets?size={page_size}", f"{API}/datasets?size={page_size}&page=1"):
        try:
            items = _items(_request_json(session, url))
        except Exception:
            continue
        if items:
            return items
    raise RuntimeError("data.gouv.ci catalog API did not return datasets")


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


def data_gouv_ci_resource(
    *,
    dataset_ids: list[str] | None = None,
    limit: int | None = None,
    force: bool = False,
    user_agent: str = "IvoireData/0.8",
    snapshot_dir: Path | None = None,
    metadata_base: dict[str, Any] | None = None,
):
    import dlt
    import requests

    base = dict(metadata_base or {})

    @dlt.resource(name="data_gouv_ci", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent})
        state = dlt.current.resource_state().setdefault("dataset_signatures", {})
        catalog = _discover(session)
        wanted = set(dataset_ids or [])
        selected = []
        for meta in catalog:
            dsid = _dataset_id(meta)
            identifiers = {dsid, meta.get("slug")} if dsid else set()
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
                row.update(classified)
                yield dlt.mark.with_table_name(row, "datagouv_catalog")

        for meta in selected:
            dsid = _dataset_id(meta)
            assert dsid is not None
            sig = _signature(meta)
            if not force and state.get(dsid) == sig:
                continue
            url = f"{API}/datasets/{quote(dsid, safe='')}/full"
            try:
                r = session.get(url, timeout=180, headers={"Accept": "text/csv,application/csv,text/plain,*/*;q=0.5"})
                r.raise_for_status()
            except Exception as exc:
                print(f"[data_gouv_ci] dataset ignoré {dsid} -> {exc}", flush=True)
                continue
            if "text/html" in r.headers.get("content-type", "").lower() and r.content.lstrip().startswith(b"<"):
                print(f"[data_gouv_ci] dataset ignoré {dsid} -> /full returned HTML", flush=True)
                continue
            table = _safe_table(dsid)
            snapshot = save_snapshot(
                snapshot_dir,
                source_id="civ_datagouv_catalog",
                url=url,
                content=r.content,
                content_type=r.headers.get("content-type"),
                name=f"{dsid}.csv",
            )
            raw_sha = str(snapshot["sha256"])
            try:
                parsed_rows = list(enumerate(_rows(r.content)))
            except Exception as exc:
                print(f"[data_gouv_ci] dataset non parsable {dsid} -> {exc}", flush=True)
                state[dsid] = sig
                continue
            classified = classifications.get(dsid) or _classification(meta, dsid, base)
            for idx, row in parsed_rows:
                row.update({
                    "__ivoiredata_dataset_id": dsid,
                    "__ivoiredata_source_url": f"{PORTAL}/datasets/{dsid}",
                    "__ivoiredata_row_index": idx,
                    "__ivoiredata_raw_sha256": raw_sha,
                    "__ivoiredata_raw_path": snapshot.get("local_path"),
                    **classified,
                })
                yield dlt.mark.with_table_name(row, table)
            state[dsid] = sig

    return resource()
