#!/usr/bin/env python3
"""Materialize the public data.gouv.ci Data Fair catalog into a local AI-ready lake.

Outputs per dataset:
  raw/data_gouv_ci/<id>/full.csv
  metadata/data_gouv_ci/<id>.json
  processed/data_gouv_ci/jsonl/<id>.jsonl
  processed/data_gouv_ci/parquet/<id>.parquet   (when pandas+pyarrow are installed)

A global manifest is written to manifests/data_gouv_ci.jsonl.
No authentication, CAPTCHA, paywall or access-control bypass is attempted.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterable

PORTAL = "https://data.gouv.ci"
API = f"{PORTAL}/data-fair/api/v1"
UA = "IvoireData/0.3 (+https://github.com/bozz33/IvoireData)"


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request(url: str, *, timeout: int = 120, accept: str = "*/*") -> tuple[bytes, dict[str, str], str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), {k.lower(): v for k, v in r.headers.items()}, r.geturl()


def get_json(url: str, *, timeout: int = 120) -> Any:
    body, _, _ = request(url, timeout=timeout, accept="application/json")
    return json.loads(body.decode("utf-8"))


def dataset_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "data", "items", "datasets"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def id_from_meta(meta: dict[str, Any]) -> str | None:
    for key in ("id", "slug", "name"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def discover_via_api(api: str, page_size: int) -> list[dict[str, Any]]:
    """Try common Data Fair catalog pagination conventions."""
    collected: dict[str, dict[str, Any]] = {}
    candidates = [
        f"{api}/datasets?size={page_size}",
        f"{api}/datasets?size={page_size}&page=1",
    ]
    for url in candidates:
        try:
            items = dataset_items(get_json(url))
        except Exception:
            continue
        for item in items:
            dsid = id_from_meta(item)
            if dsid:
                collected[dsid] = item
        if collected:
            break
    return list(collected.values())


def discover_via_sitemap(portal: str) -> list[dict[str, Any]]:
    """Fallback: extract /datasets/<slug> entries from the portal sitemap."""
    urls = [f"{portal}/sitemap.xml", f"{portal}/sitemap"]
    slugs: set[str] = set()
    for url in urls:
        try:
            body, _, _ = request(url, accept="application/xml,text/xml,text/html")
        except Exception:
            continue
        text = body.decode("utf-8", "replace")
        for match in re.findall(r"https?://[^<\"'\s]+/datasets/([A-Za-z0-9_%.'()\-]+)", text):
            slugs.add(urllib.parse.unquote(match).rstrip("/"))
        for match in re.findall(r"href=[\"'](?:https?://[^/]+)?/datasets/([^\"'#?]+)", text, flags=re.I):
            slugs.add(urllib.parse.unquote(match).rstrip("/"))
        if slugs:
            break
    return [{"id": slug} for slug in sorted(slugs)]


def discover_catalog(portal: str, api: str, page_size: int) -> list[dict[str, Any]]:
    items = discover_via_api(api, page_size)
    if len(items) < 20:
        fallback = discover_via_sitemap(portal)
        by_id = {id_from_meta(x): x for x in items if id_from_meta(x)}
        for item in fallback:
            by_id.setdefault(item["id"], item)
        items = list(by_id.values())
    return sorted(items, key=lambda x: id_from_meta(x) or "")


def safe_id(value: str) -> str:
    value = value.strip().replace("/", "-")
    value = re.sub(r"[^A-Za-z0-9._()'\-]+", "-", value)
    return value[:180] or "dataset"


def flatten_owner(meta: dict[str, Any]) -> str | None:
    for key in ("ownerName", "producer", "publisher", "organization"):
        v = meta.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    owner = meta.get("owner")
    if isinstance(owner, dict):
        for key in ("name", "title", "id"):
            v = owner.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def license_value(meta: dict[str, Any]) -> str | None:
    value = meta.get("license") or meta.get("licenseName")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("title") or value.get("name") or value.get("id")
    return None


def title_value(meta: dict[str, Any], dsid: str) -> str:
    for key in ("title", "name"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return dsid


def fetch_metadata(api: str, dsid: str, seed: dict[str, Any]) -> dict[str, Any]:
    try:
        meta = get_json(f"{api}/datasets/{urllib.parse.quote(dsid, safe='')}")
        if isinstance(meta, dict):
            merged = dict(seed)
            merged.update(meta)
            return merged
    except Exception:
        pass
    return dict(seed)


def fetch_full_csv(api: str, dsid: str) -> tuple[bytes, str, dict[str, str]]:
    url = f"{api}/datasets/{urllib.parse.quote(dsid, safe='')}/full"
    body, headers, final_url = request(url, accept="text/csv,application/csv,text/plain,*/*;q=0.5")
    content_type = headers.get("content-type", "").lower()
    if "text/html" in content_type and body.lstrip().startswith(b"<"):
        raise RuntimeError("/full returned HTML instead of tabular data")
    return body, final_url, headers


def decode_csv(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def dict_rows(data: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    text = decode_csv(data)
    sample = text[:65536]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    fields = list(reader.fieldnames or [])
    rows: list[dict[str, Any]] = []
    for row in reader:
        rows.append({str(k): v for k, v in row.items() if k is not None})
    return fields, rows


def add_provenance(rows: Iterable[dict[str, Any]], *, dsid: str, meta: dict[str, Any], source_url: str, retrieved_at: str) -> list[dict[str, Any]]:
    title = title_value(meta, dsid)
    provider = flatten_owner(meta)
    license_name = license_value(meta)
    out = []
    for idx, row in enumerate(rows):
        r = dict(row)
        r.update({
            "__ivoiredata_dataset_id": dsid,
            "__ivoiredata_dataset_title": title,
            "__ivoiredata_provider": provider,
            "__ivoiredata_license": license_name,
            "__ivoiredata_source_url": source_url,
            "__ivoiredata_retrieved_at": retrieved_at,
            "__ivoiredata_row_index": idx,
        })
        out.append(r)
    return out


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def write_jsonl(path: pathlib.Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            count += 1
    return count


def write_parquet(path: pathlib.Path, rows: list[dict[str, Any]]) -> tuple[bool, str | None]:
    try:
        import pandas as pd
    except ImportError:
        return False, "pandas not installed"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(path, index=False)
        return True, None
    except Exception as exc:
        return False, str(exc)


def materialize_one(api: str, root: pathlib.Path, seed: dict[str, Any], *, parquet: bool, delay: float) -> dict[str, Any]:
    dsid0 = id_from_meta(seed)
    if not dsid0:
        raise ValueError("dataset has no id/slug")
    dsid = safe_id(dsid0)
    retrieved_at = utcnow()
    meta = fetch_metadata(api, dsid0, seed)
    meta_url = f"{api}/datasets/{urllib.parse.quote(dsid0, safe='')}"
    raw_dir = root / "raw" / "data_gouv_ci" / dsid
    meta_path = root / "metadata" / "data_gouv_ci" / f"{dsid}.json"
    jsonl_path = root / "processed" / "data_gouv_ci" / "jsonl" / f"{dsid}.jsonl"
    parquet_path = root / "processed" / "data_gouv_ci" / "parquet" / f"{dsid}.parquet"
    raw_dir.mkdir(parents=True, exist_ok=True)
    write_json(meta_path, {"retrieved_at": retrieved_at, "metadata_url": meta_url, "metadata": meta})

    body, source_url, headers = fetch_full_csv(api, dsid0)
    raw_path = raw_dir / "full.csv"
    raw_path.write_bytes(body)
    fields, rows0 = dict_rows(body)
    rows = add_provenance(rows0, dsid=dsid0, meta=meta, source_url=source_url, retrieved_at=retrieved_at)
    row_count = write_jsonl(jsonl_path, rows)
    pq_ok, pq_error = (write_parquet(parquet_path, rows) if parquet else (False, "disabled"))
    manifest = {
        "dataset_id": dsid0,
        "title": title_value(meta, dsid0),
        "provider": flatten_owner(meta),
        "license": license_value(meta),
        "retrieved_at": retrieved_at,
        "metadata_url": meta_url,
        "source_url": source_url,
        "raw_path": str(raw_path.relative_to(root)),
        "raw_sha256": sha256(body),
        "raw_bytes": len(body),
        "content_type": headers.get("content-type"),
        "columns": fields,
        "row_count": row_count,
        "jsonl_path": str(jsonl_path.relative_to(root)),
        "parquet_path": str(parquet_path.relative_to(root)) if pq_ok else None,
        "parquet_error": pq_error,
        "status": "ok",
    }
    if delay:
        time.sleep(delay)
    return manifest


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=pathlib.Path, default=pathlib.Path("data_lake"))
    p.add_argument("--portal", default=PORTAL)
    p.add_argument("--api", default=API)
    p.add_argument("--page-size", type=int, default=1000, help="catalog request size")
    p.add_argument("--limit", type=int, default=0, help="materialize only first N datasets; 0 means all")
    p.add_argument("--dataset", action="append", default=[], help="materialize only this dataset id/slug; repeatable")
    p.add_argument("--no-parquet", action="store_true")
    p.add_argument("--catalog-only", action="store_true")
    p.add_argument("--delay", type=float, default=0.15, help="polite delay between datasets")
    args = p.parse_args()

    root: pathlib.Path = args.output
    root.mkdir(parents=True, exist_ok=True)
    catalog = discover_catalog(args.portal.rstrip("/"), args.api.rstrip("/"), max(100, args.page_size))
    if args.dataset:
        wanted = set(args.dataset)
        catalog = [x for x in catalog if id_from_meta(x) in wanted]
        missing = wanted - {id_from_meta(x) for x in catalog}
        catalog.extend({"id": x} for x in sorted(missing))
    if args.limit > 0:
        catalog = catalog[: args.limit]

    catalog_path = root / "catalog" / "data_gouv_ci.json"
    write_json(catalog_path, {"retrieved_at": utcnow(), "count": len(catalog), "datasets": catalog})
    print(f"catalog: {len(catalog)} datasets -> {catalog_path}")
    if args.catalog_only:
        return 0

    manifest_path = root / "manifests" / "data_gouv_ci.jsonl"
    errors_path = root / "manifests" / "data_gouv_ci_errors.jsonl"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    ok = failed = rows_total = bytes_total = 0
    with manifest_path.open("w", encoding="utf-8") as mf, errors_path.open("w", encoding="utf-8") as ef:
        for i, seed in enumerate(catalog, 1):
            dsid = id_from_meta(seed) or f"unknown-{i}"
            try:
                item = materialize_one(args.api.rstrip("/"), root, seed, parquet=not args.no_parquet, delay=args.delay)
                mf.write(json.dumps(item, ensure_ascii=False) + "\n")
                mf.flush()
                ok += 1
                rows_total += int(item.get("row_count") or 0)
                bytes_total += int(item.get("raw_bytes") or 0)
                print(f"[{i}/{len(catalog)}] OK {dsid}: {item['row_count']} rows")
            except Exception as exc:
                failed += 1
                err = {"dataset_id": dsid, "retrieved_at": utcnow(), "status": "error", "error": repr(exc)}
                ef.write(json.dumps(err, ensure_ascii=False) + "\n")
                ef.flush()
                print(f"[{i}/{len(catalog)}] ERROR {dsid}: {exc}", file=sys.stderr)

    summary = {
        "generated_at": utcnow(),
        "catalog_count": len(catalog),
        "materialized_ok": ok,
        "materialized_failed": failed,
        "rows_total": rows_total,
        "raw_bytes_total": bytes_total,
        "manifest": str(manifest_path.relative_to(root)),
        "errors": str(errors_path.relative_to(root)),
    }
    write_json(root / "manifests" / "data_gouv_ci_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if ok > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
