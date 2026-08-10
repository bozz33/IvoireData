from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from ..state_io import atomic_write_json, load_json
from ..upstream_state import UpstreamState

CATALOG_URL = "https://bulks-faostat.fao.org/production/datasets_E.json"

# Kept for backward compatibility and stable table names during the v0.8.1 -> v0.8.2
# migration. v0.8.2 discovers current domains from the official FAO catalogue instead
# of freezing collection to these five files.
DEFAULT_DATASETS = (
    ("production_crops_livestock", "https://bulks-faostat.fao.org/production/Production_Crops_Livestock_E_All_Data_(Normalized).zip"),
    ("food_security", "https://bulks-faostat.fao.org/production/Food_Security_Data_E_All_Data_(Normalized).zip"),
    ("prices", "https://bulks-faostat.fao.org/production/Prices_E_All_Data_(Normalized).zip"),
    ("land_use", "https://bulks-faostat.fao.org/production/Inputs_LandUse_E_All_Data_(Normalized).zip"),
    ("trade_crops_livestock", "https://bulks-faostat.fao.org/production/Trade_CropsLivestock_E_All_Data_(Normalized).zip"),
)

LEGACY_TABLE_BY_CODE = {
    "QCL": "production_crops_livestock",
    "FS": "food_security",
    "PP": "prices",
    "RL": "land_use",
    "TCL": "trade_crops_livestock",
}

DEFAULT_ALIASES = (
    "Côte d'Ivoire",
    "Cote d'Ivoire",
    "Côte d’Ivoire",
    "Ivory Coast",
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_table(value: str) -> str:
    return "faostat_" + re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()[:100]


def _table_name(dataset_code: str) -> str:
    return _safe_table(LEGACY_TABLE_BY_CODE.get(dataset_code, dataset_code))


def _matches_country(row: dict[str, str], aliases: set[str]) -> bool:
    candidates = (
        row.get("Area"), row.get("Area Name"), row.get("Country"), row.get("Country Name"),
        row.get("Geographic Area"), row.get("Reporter Country"), row.get("Reporter Countries"),
        row.get("Reporting Country"), row.get("Recipient Country"), row.get("Recipient"),
    )
    normalized = {a.casefold().strip() for a in aliases}
    return any((value or "").casefold().strip() in normalized for value in candidates)


def _catalog_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    datasets = payload.get("Datasets")
    if isinstance(datasets, dict):
        value = datasets.get("Dataset")
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            return [value]
    return []


def _signature(meta: dict[str, Any]) -> str:
    compact = {
        key: meta.get(key)
        for key in ("DatasetCode", "DateUpdate", "FileSize", "FileRows", "FileLocation", "FileType", "CompressionFormat")
    }
    return hashlib.sha256(json.dumps(compact, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def _file_size_bytes(value: Any) -> int | None:
    text = str(value or "").strip().upper().replace(" ", "")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(KB|MB|GB|B)?", text)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2) or "B"
    multiplier = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}[unit]
    return int(number * multiplier)


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _download_to_temp(session, url: str, *, max_bytes: int) -> tuple[Path, str, int, str | None, str | None, str | None]:
    response = session.get(url, timeout=600, stream=True)
    response.raise_for_status()
    content_length = int(response.headers.get("content-length") or 0)
    if content_length and content_length > max_bytes:
        raise RuntimeError(f"FAOSTAT payload too large ({content_length} > {max_bytes}): {url}")
    digest = hashlib.sha256()
    size = 0
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        with tmp:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise RuntimeError(f"FAOSTAT payload exceeded max_bytes ({max_bytes}): {url}")
                digest.update(chunk)
                tmp.write(chunk)
        return (
            Path(tmp.name), digest.hexdigest(), size, response.headers.get("content-type"),
            response.headers.get("etag"), response.headers.get("last-modified"),
        )
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def _save_download(tmp_path: Path, *, directory: Path | None, source_id: str, url: str,
                   content_type: str | None, digest: str, size_bytes: int) -> dict[str, object]:
    result: dict[str, object] = {"sha256": digest, "size_bytes": size_bytes, "source_url": url}
    if directory is None:
        return result
    directory.mkdir(parents=True, exist_ok=True)
    name = Path(urlparse(url).path).name or f"{source_id}.zip"
    target = directory / f"{Path(name).stem}--{digest[:16]}{Path(name).suffix or '.zip'}"
    if not target.exists():
        shutil.copy2(tmp_path, target)
    meta = {
        "source_id": source_id,
        "source_url": url,
        "content_type": content_type,
        "sha256": digest,
        "size_bytes": size_bytes,
        "retrieved_at": _now(),
        "local_file": target.name,
    }
    atomic_write_json(target.with_suffix(target.suffix + ".meta.json"), meta)
    result["local_path"] = str(target)
    return result


def _existing_snapshot(snapshot_dir: Path | None, url: str, update_at: Any) -> dict[str, Any] | None:
    """Find a legacy snapshot fetched after the official DateUpdate for migration."""
    if snapshot_dir is None or not snapshot_dir.exists():
        return None
    official_update = _parse_time(update_at)
    newest: tuple[datetime, dict[str, Any], Path] | None = None
    for sidecar in snapshot_dir.glob("*.zip.meta.json"):
        meta = load_json(sidecar, {})
        if not isinstance(meta, dict) or meta.get("source_url") != url:
            continue
        retrieved = _parse_time(meta.get("retrieved_at"))
        if retrieved is None or (official_update is not None and retrieved < official_update):
            continue
        local = sidecar.with_name(sidecar.name[: -len(".meta.json")])
        if not local.exists():
            continue
        if newest is None or retrieved > newest[0]:
            newest = (retrieved, meta, local)
    if newest is None:
        return None
    meta = dict(newest[1])
    meta["local_path"] = str(newest[2])
    return meta


def _table_has_parquet(tables_dir: Path | None, table: str) -> bool:
    if tables_dir is None:
        return False
    root = tables_dir / "data" / table
    return root.exists() and any(root.rglob("*.parquet"))


def _country_rows_from_zip(path: Path, aliases: set[str]) -> Iterable[dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv") and "normalized" in name.lower()]
        if not csv_names:
            csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not csv_names:
            raise RuntimeError(f"FAOSTAT archive has no CSV: {path.name}")
        for csv_name in csv_names:
            with archive.open(csv_name) as binary:
                text = io.TextIOWrapper(binary, encoding="utf-8-sig", errors="replace", newline="")
                reader = csv.DictReader(text)
                for row in reader:
                    cleaned = {str(k): v for k, v in row.items() if k is not None}
                    if _matches_country(cleaned, aliases):
                        yield cleaned


def faostat_country_resource(
    *,
    country: str = "CIV",
    aliases: Iterable[str] = DEFAULT_ALIASES,
    datasets: Iterable[dict[str, str] | tuple[str, str]] | None = None,
    dataset_codes: Iterable[str] | None = None,
    catalog_url: str = CATALOG_URL,
    include_discontinued: bool = False,
    user_agent: str = "IvoireData/0.8.2",
    snapshot_dir: Path | None = None,
    tables_dir: Path | None = None,
    upstream_state_path: Path | None = None,
    max_bytes_per_file: int = 500_000_000,
    max_new_bytes_per_run: int = 1_500_000_000,
):
    """Synchronize current official FAOSTAT domains incrementally for Côte d'Ivoire.

    The tiny official `datasets_E.json` catalogue is fetched on each due check. Its
    DatasetCode + DateUpdate + file metadata forms a stable upstream signature. A ZIP is
    transferred only for a new/changed domain. Existing v0.8.1 snapshots for the five
    legacy domains are adopted when they were retrieved after FAO's DateUpdate.

    `datasets` remains a compatibility escape hatch for explicit old-style lists; the
    default is the current official catalogue (excluding discontinued archives).
    """
    import dlt
    import requests

    aliases_set = set(aliases)
    wanted_codes = {str(code).strip().upper() for code in (dataset_codes or []) if str(code).strip()}

    @dlt.resource(name="faostat_civ", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent})
        loaded_signatures = dlt.current.resource_state().setdefault("dataset_signatures_v082", {})
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None

        if datasets is not None:
            catalog: list[dict[str, Any]] = []
            for index, item in enumerate(datasets):
                if isinstance(item, dict):
                    name, url = str(item["name"]), str(item["url"])
                else:
                    name, url = map(str, item)
                catalog.append({
                    "DatasetCode": f"LEGACY_{index}", "DatasetName": name,
                    "DateUpdate": None, "FileSize": None, "FileRows": None, "FileLocation": url,
                })
            catalog_snapshot = None
        else:
            response = session.get(catalog_url, timeout=120, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
            catalog = _catalog_rows(payload)
            if not catalog:
                raise RuntimeError("FAOSTAT official bulk catalogue returned no datasets")
            catalog_snapshot = {
                "sha256": hashlib.sha256(response.content).hexdigest(),
                "size_bytes": len(response.content),
                "source_url": response.url,
            }
            if snapshot_dir is not None:
                catalog_file = snapshot_dir / "faostat-datasets_E.json"
                if not catalog_file.exists() or catalog_file.read_bytes() != response.content:
                    snapshot_dir.mkdir(parents=True, exist_ok=True)
                    temp = catalog_file.with_suffix(".json.tmp")
                    temp.write_bytes(response.content)
                    os.replace(temp, catalog_file)

        selected: list[dict[str, Any]] = []
        for meta in catalog:
            code = str(meta.get("DatasetCode") or "").strip().upper()
            name = str(meta.get("DatasetName") or "")
            url = str(meta.get("FileLocation") or "")
            if not code or not url:
                continue
            if wanted_codes and code not in wanted_codes:
                continue
            if not include_discontinued and name.casefold().startswith("discontinued archives and data series:"):
                continue
            selected.append(meta)

        stats: dict[str, Any] = {
            "catalog_url": catalog_url,
            "catalog_datasets": len(catalog),
            "selected_current_datasets": len(selected),
            "catalog_sha256": catalog_snapshot.get("sha256") if catalog_snapshot else None,
            "unchanged": 0,
            "adopted_v081": 0,
            "downloaded": 0,
            "replayed_from_local_cache": 0,
            "with_country_rows": 0,
            "without_country_rows": 0,
            "failed": 0,
            "skipped_oversize": 0,
            "deferred_budget": 0,
            "business_rows": 0,
            "downloaded_bytes": 0,
            "backlog": [],
            "failures": [],
        }
        budget_remaining = max(0, int(max_new_bytes_per_run))

        # Full official catalogue metadata is useful downstream even when a domain does
        # not contain CIV rows.
        for meta in selected:
            row = dict(meta)
            row["__ivoiredata_country"] = country
            row["__ivoiredata_dataset_signature"] = _signature(meta)
            yield dlt.mark.with_table_name(row, "faostat_catalog")

        for meta in selected:
            code = str(meta["DatasetCode"]).strip().upper()
            url = str(meta["FileLocation"])
            signature = _signature(meta)
            artifact = f"dataset:{code}"
            table = _table_name(code)

            if loaded_signatures.get(code) == signature:
                stats["unchanged"] += 1
                if upstream:
                    upstream.mark_unchanged("civ_faostat", artifact, signature=signature, url=url)
                continue

            # One-time migration: if v0.8.1 already downloaded the current version and
            # its legacy table exists, adopt it without transferring the same ZIP again.
            legacy = _existing_snapshot(snapshot_dir, url, meta.get("DateUpdate"))
            if legacy is not None and _table_has_parquet(tables_dir, table):
                loaded_signatures[code] = signature
                stats["adopted_v081"] += 1
                if upstream:
                    upstream.mark_downloaded(
                        "civ_faostat", artifact, url=url, signature=signature,
                        sha256=str(legacy.get("sha256") or "") or None,
                        size_bytes=int(legacy.get("size_bytes") or 0), method="ADOPTED_V081",
                        local_path=str(legacy.get("local_path") or "") or None,
                    )
                continue

            cached_path = upstream.cached_path("civ_faostat", artifact, signature) if upstream else None
            tmp_path: Path | None = None
            local_zip: Path | None = cached_path
            snapshot: dict[str, Any] | None = None
            from_network = False

            estimated = _file_size_bytes(meta.get("FileSize"))
            if cached_path is None:
                if estimated is not None and estimated > max_bytes_per_file:
                    stats["skipped_oversize"] += 1
                    stats["backlog"].append({"DatasetCode": code, "reason": "FILE_TOO_LARGE", "estimated_bytes": estimated, "url": url})
                    continue
                if estimated is not None and estimated > budget_remaining:
                    stats["deferred_budget"] += 1
                    stats["backlog"].append({"DatasetCode": code, "reason": "RUN_BUDGET", "estimated_bytes": estimated, "url": url})
                    continue
                try:
                    tmp_path, digest, size, content_type, etag, last_modified = _download_to_temp(
                        session, url, max_bytes=max_bytes_per_file
                    )
                    snapshot = _save_download(
                        tmp_path,
                        directory=snapshot_dir,
                        source_id="civ_faostat",
                        url=url,
                        content_type=content_type,
                        digest=digest,
                        size_bytes=size,
                    )
                    local_zip = Path(str(snapshot.get("local_path"))) if snapshot.get("local_path") else tmp_path
                    budget_remaining = max(0, budget_remaining - size)
                    stats["downloaded"] += 1
                    stats["downloaded_bytes"] += size
                    from_network = True
                    if upstream:
                        upstream.mark_downloaded(
                            "civ_faostat", artifact, url=url, signature=signature,
                            sha256=digest, size_bytes=size, etag=etag, last_modified=last_modified,
                            method="OFFICIAL_BULK_ZIP", local_path=str(local_zip),
                        )
                except Exception as exc:
                    stats["failed"] += 1
                    stats["failures"].append({"DatasetCode": code, "error": str(exc)[:1000], "url": url})
                    if upstream:
                        upstream.mark_error("civ_faostat", artifact, url=url, error=str(exc), method="OFFICIAL_BULK_ZIP")
                    if tmp_path is not None:
                        tmp_path.unlink(missing_ok=True)
                    continue
            else:
                cached = upstream.get("civ_faostat", artifact) if upstream else {}
                snapshot = {
                    "sha256": cached.get("sha256"), "size_bytes": cached.get("size_bytes"),
                    "local_path": str(cached_path), "source_url": url,
                }
                stats["replayed_from_local_cache"] += 1

            try:
                assert local_zip is not None
                country_rows = list(_country_rows_from_zip(local_zip, aliases_set))
                if country_rows:
                    stats["with_country_rows"] += 1
                    stats["business_rows"] += len(country_rows)
                else:
                    stats["without_country_rows"] += 1
                for cleaned in country_rows:
                    cleaned["__ivoiredata_country"] = country
                    cleaned["__ivoiredata_dataset"] = code
                    cleaned["__ivoiredata_dataset_name"] = meta.get("DatasetName")
                    cleaned["__ivoiredata_dataset_update"] = meta.get("DateUpdate")
                    cleaned["__ivoiredata_source_url"] = url
                    cleaned["__ivoiredata_raw_sha256"] = snapshot.get("sha256") if snapshot else None
                    cleaned["__ivoiredata_raw_path"] = snapshot.get("local_path") if snapshot else str(local_zip)
                    yield dlt.mark.with_table_name(cleaned, table)
                loaded_signatures[code] = signature
            except Exception as exc:
                stats["failed"] += 1
                stats["failures"].append({"DatasetCode": code, "error": f"parse: {exc}"[:1000], "url": url})
                if upstream:
                    upstream.mark_error("civ_faostat", artifact, url=url, error=f"parse: {exc}", method="OFFICIAL_BULK_ZIP")
            finally:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)

        stats["backlog_count"] = len(stats["backlog"]) + stats["failed"]
        if snapshot_dir:
            atomic_write_json(snapshot_dir / "faostat_sync_stats.json", stats)
        yield dlt.mark.with_table_name({"run_stats_json": json.dumps(stats, ensure_ascii=False), **stats}, "faostat_sync_stats")

    return resource()
