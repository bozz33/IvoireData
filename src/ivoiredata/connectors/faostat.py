from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

DEFAULT_DATASETS = (
    ("production_crops_livestock", "https://bulks-faostat.fao.org/production/Production_Crops_Livestock_E_All_Data_(Normalized).zip"),
    ("food_security", "https://bulks-faostat.fao.org/production/Food_Security_Data_E_All_Data_(Normalized).zip"),
    ("prices", "https://bulks-faostat.fao.org/production/Prices_E_All_Data_(Normalized).zip"),
    ("land_use", "https://bulks-faostat.fao.org/production/Inputs_LandUse_E_All_Data_(Normalized).zip"),
    ("trade_crops_livestock", "https://bulks-faostat.fao.org/production/Trade_CropsLivestock_E_All_Data_(Normalized).zip"),
)

DEFAULT_ALIASES = (
    "Côte d'Ivoire",
    "Cote d'Ivoire",
    "Côte d’Ivoire",
    "Ivory Coast",
)


def _safe_table(value: str) -> str:
    return "faostat_" + "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")


def _matches_country(row: dict[str, str], aliases: set[str]) -> bool:
    candidates = (
        row.get("Area"),
        row.get("Area Name"),
        row.get("Country"),
        row.get("Country Name"),
        row.get("Geographic Area"),
    )
    normalized = {a.casefold().strip() for a in aliases}
    return any((value or "").casefold().strip() in normalized for value in candidates)


def _save_download(
    tmp_path: Path,
    *,
    directory: Path | None,
    source_id: str,
    url: str,
    content_type: str | None,
    digest: str,
    size_bytes: int,
) -> dict[str, object]:
    result: dict[str, object] = {
        "sha256": digest,
        "size_bytes": size_bytes,
        "source_url": url,
    }
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
        "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "local_file": target.name,
    }
    target.with_suffix(target.suffix + ".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result["local_path"] = str(target)
    return result


def _download_to_temp(session, url: str, *, max_bytes: int) -> tuple[Path, str, int, str | None]:
    response = session.get(url, timeout=300, stream=True)
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
        return Path(tmp.name), digest.hexdigest(), size, response.headers.get("content-type")
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def faostat_country_resource(
    *,
    country: str = "CIV",
    aliases: Iterable[str] = DEFAULT_ALIASES,
    datasets: Iterable[dict[str, str] | tuple[str, str]] = DEFAULT_DATASETS,
    user_agent: str = "IvoireData/0.7",
    snapshot_dir: Path | None = None,
    max_bytes_per_file: int = 400_000_000,
):
    """Load selected official FAOSTAT bulk datasets and keep only Côte d'Ivoire rows.

    FAOSTAT bulk files are global. IvoireData snapshots the upstream ZIP locally, then
    streams the normalized CSV inside each archive and emits only rows matching the
    configured country aliases. This keeps the delivered Parquet country-specific while
    preserving the official raw payload for provenance.
    """
    import dlt
    import requests

    aliases_set = set(aliases)
    dataset_specs: list[tuple[str, str]] = []
    for item in datasets:
        if isinstance(item, dict):
            dataset_specs.append((str(item["name"]), str(item["url"])))
        else:
            name, url = item
            dataset_specs.append((str(name), str(url)))

    @dlt.resource(name="faostat_civ", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent, "Accept": "application/zip,*/*;q=0.8"})
        total_country_rows = 0
        for dataset_name, url in dataset_specs:
            tmp_path = None
            try:
                tmp_path, digest, size, content_type = _download_to_temp(
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
                yield dlt.mark.with_table_name(
                    {
                        "dataset": dataset_name,
                        "source_url": url,
                        "raw_sha256": digest,
                        "raw_path": snapshot.get("local_path"),
                        "size_bytes": size,
                    },
                    "faostat_catalog",
                )
                with zipfile.ZipFile(tmp_path) as archive:
                    csv_names = [
                        n for n in archive.namelist()
                        if n.lower().endswith(".csv") and "normalized" in n.lower()
                    ]
                    if not csv_names:
                        csv_names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
                    if not csv_names:
                        raise RuntimeError(f"FAOSTAT archive has no CSV: {url}")
                    for csv_name in csv_names:
                        with archive.open(csv_name) as binary:
                            text = io.TextIOWrapper(binary, encoding="utf-8-sig", errors="replace", newline="")
                            reader = csv.DictReader(text)
                            for row in reader:
                                cleaned = {str(k): v for k, v in row.items() if k is not None}
                                if not _matches_country(cleaned, aliases_set):
                                    continue
                                cleaned["__ivoiredata_country"] = country
                                cleaned["__ivoiredata_dataset"] = dataset_name
                                cleaned["__ivoiredata_source_url"] = url
                                cleaned["__ivoiredata_raw_sha256"] = digest
                                cleaned["__ivoiredata_raw_path"] = snapshot.get("local_path")
                                total_country_rows += 1
                                yield dlt.mark.with_table_name(cleaned, _safe_table(dataset_name))
            finally:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
        if total_country_rows == 0:
            raise RuntimeError("FAOSTAT bulk downloads succeeded but no Côte d'Ivoire rows were found")

    return resource()
