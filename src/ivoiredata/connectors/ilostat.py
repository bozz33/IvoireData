from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

from ..snapshots import save_snapshot
from ..state_io import atomic_write_json
from ..upstream_state import UpstreamState

TOC_API = "https://rplumber.ilo.org/metadata/toc/indicator/"
REF_AREA_TOC_API = "https://rplumber.ilo.org/metadata/toc/ref_area/"
DATA_API = "https://rplumber.ilo.org/data/indicator/"
CSV_BASE = DATA_API


class RdsParseError(RuntimeError):
    """Legacy compatibility error for the retired unsafe in-process RDS path."""


def _read_rds_rows(path: Path):
    raise RdsParseError(f"RDS parsing disabled, use official ILOSTAT REST API ({path.name})")


def _csv_rows(content: bytes) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig", "replace")))
    return [{str(k): v for k, v in row.items() if k is not None} for row in reader]


def _toc_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "items", "records", "value"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        if payload and all(isinstance(value, dict) for value in payload.values()):
            return [dict(value) for value in payload.values()]
    return []


def _indicator_id(row: dict[str, Any]) -> str | None:
    for key in ("id", "indicator", "indicator_code"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _signature(row: dict[str, Any]) -> str:
    compact = {
        key: row.get(key)
        for key in ("id", "indicator", "freq", "size", "data.start", "data.end", "last.update", "n.records", "collection")
    }
    return hashlib.sha256(json.dumps(compact, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def _ref_area_signature(rows: list[dict[str, Any]]) -> str:
    compact = [
        {
            key: row.get(key)
            for key in ("id", "ref_area", "freq", "size", "data.start", "data.end", "last.update", "n.records")
        }
        for row in sorted(rows, key=lambda item: str(item.get("id") or ""))
    ]
    return hashlib.sha256(json.dumps(compact, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def _country_ref_rows(rows: list[dict[str, Any]], country: str, wanted_freqs: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    prefix = country.upper() + "_"
    for row in rows:
        ref_area = str(row.get("ref_area") or "").upper()
        rid = str(row.get("id") or "").upper()
        freq = str(row.get("freq") or (rid.rsplit("_", 1)[-1] if "_" in rid else "")).upper()
        if ref_area != country.upper() and not rid.startswith(prefix):
            continue
        if wanted_freqs and freq not in wanted_freqs:
            continue
        out.append(row)
    return out


def _table_name(indicator_id: str) -> str:
    return "ilostat_" + re.sub(r"[^a-zA-Z0-9]+", "_", indicator_id).strip("_").lower()[:100]


def _get_retry(session, url: str, *, params: dict[str, Any] | None = None,
               headers: dict[str, str] | None = None, timeout: int = 240, attempts: int = 4):
    import requests

    last_exc: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            response = session.get(url, params=params, headers=headers, timeout=timeout)
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                if attempt + 1 >= attempts:
                    response.raise_for_status()
                retry_after = response.headers.get("retry-after")
                try:
                    delay = float(retry_after) if retry_after else min(8.0, 2.0**attempt)
                except ValueError:
                    delay = min(8.0, 2.0**attempt)
                time.sleep(max(0.0, delay))
                continue
            response.raise_for_status()
            return response
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(8.0, 2.0**attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"ILOSTAT request failed: {url}")


def _fetch_toc_url(session, url: str, *, timeout: int = 120) -> tuple[Any, list[dict[str, Any]]]:
    response = _get_retry(session, url, params={"lang": "en"}, timeout=timeout, headers={"Accept": "application/json"})
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"ILOSTAT TOC did not return JSON: {url}") from exc
    rows = _toc_rows(payload)
    if not rows:
        raise RuntimeError(f"ILOSTAT TOC returned zero rows: {url}")
    return response, rows


def _fetch_toc(session, *, timeout: int = 120) -> tuple[Any, list[dict[str, Any]]]:
    return _fetch_toc_url(session, TOC_API, timeout=timeout)


def _fetch_ref_area_toc(session, *, timeout: int = 120) -> tuple[Any, list[dict[str, Any]]]:
    return _fetch_toc_url(session, REF_AREA_TOC_API, timeout=timeout)


def _fetch_indicator(session, indicator_id: str, country: str, *, data_api: str = DATA_API, timeout: int = 240):
    params = {
        "id": indicator_id,
        "ref_area": country,
        "lang": "en",
        "type": "code",
        "format": ".csv",
    }
    response = _get_retry(
        session, data_api.rstrip("/") + "/", params=params, timeout=timeout,
        headers={"Accept": "text/csv,text/plain,*/*;q=0.5"},
    )
    ctype = response.headers.get("content-type", "").lower()
    if "html" in ctype and response.content.lstrip().startswith(b"<"):
        raise RuntimeError(f"ILOSTAT returned HTML for indicator={indicator_id}")
    rows = _csv_rows(response.content)
    filtered = [row for row in rows if not row.get("ref_area") or row.get("ref_area") == country]
    return response, filtered


def ilostat_ref_area_resource(
    *,
    country: str = "CIV",
    frequencies: Iterable[str] = (),
    base_url: str = DATA_API,
    user_agent: str = "IvoireData/0.8.3",
    snapshot_dir: Path | None = None,
    upstream_state_path: Path | None = None,
    request_pause_seconds: float = 0.05,
):
    """Synchronize broad ILOSTAT country coverage from official metadata + safe CSV REST.

    ILOSTAT officially exposes data both by indicator and by ref_area. The tiny ref_area
    TOC is checked first and its CIV A/Q/M rows form a country-wide version signature.
    If that signature is unchanged, no indicator TOC/data request is made. When CIV has
    changed, the indicator TOC identifies new/updated indicators and their CSV data is
    requested with both `id=<indicator>` and `ref_area=CIV`.

    The official bulk ref_area RDS files are intentionally not parsed in-process because
    that path previously caused native SIGSEGV failures. We use their official TOC as the
    cheap change gate and the official CSV endpoint as the safe materialization path.
    """
    import dlt
    import requests

    wanted_freqs = {str(freq).strip().upper() for freq in frequencies if str(freq).strip()}
    pause = max(0.0, min(float(request_pause_seconds), 2.0))

    @dlt.resource(name="ilostat_civ", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent})
        dlt_state = dlt.current.resource_state()
        loaded_signatures = dlt_state.setdefault("indicator_signatures", {})
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None

        ref_response, ref_toc = _fetch_ref_area_toc(session)
        country_ref = _country_ref_rows(ref_toc, country, wanted_freqs)
        if not country_ref:
            raise RuntimeError(f"ILOSTAT ref_area TOC has no entries for {country}")
        country_signature = _ref_area_signature(country_ref)
        ref_snapshot = save_snapshot(
            snapshot_dir, source_id="civ_ilostat", url=ref_response.url,
            content=ref_response.content, content_type=ref_response.headers.get("content-type"),
            name="ilostat-ref-area-toc.json",
        )

        prior_country_signature = dlt_state.get("country_ref_area_signature_v083")
        if prior_country_signature == country_signature:
            stats = {
                "country": country,
                "ref_area_toc_files": len(country_ref),
                "ref_area_ids": sorted(str(row.get("id") or "") for row in country_ref),
                "ref_area_signature": country_signature,
                "ref_area_unchanged_gate": True,
                "indicator_toc_requested": False,
                "selected_indicators": 0,
                "unchanged": 0,
                "network_queries": 0,
                "replayed_from_local_cache": 0,
                "with_country_rows": 0,
                "without_country_rows": 0,
                "failed": 0,
                "business_rows": 0,
                "failures": [],
                "data_api": base_url,
            }
            if upstream:
                upstream.mark_unchanged(
                    "civ_ilostat", "ref_area:CIV", signature=country_signature,
                    url=ref_response.url, reason="ILOSTAT_REF_AREA_LAST_UPDATE",
                    extra={"ref_area_ids": stats["ref_area_ids"]},
                )
            if snapshot_dir:
                atomic_write_json(snapshot_dir / "ilostat_sync_stats.json", stats)
            yield dlt.mark.with_table_name({"run_stats_json": json.dumps(stats, ensure_ascii=False), **stats}, "ilostat_sync_stats")
            return

        toc_response, toc = _fetch_toc(session)
        toc_snapshot = save_snapshot(
            snapshot_dir,
            source_id="civ_ilostat",
            url=toc_response.url,
            content=toc_response.content,
            content_type=toc_response.headers.get("content-type"),
            name="ilostat-indicator-toc.json",
        )

        candidates: list[tuple[str, dict[str, Any], str]] = []
        for meta in toc:
            indicator_id = _indicator_id(meta)
            if not indicator_id:
                continue
            freq = str(meta.get("freq") or indicator_id.rsplit("_", 1)[-1]).upper()
            if wanted_freqs and freq not in wanted_freqs:
                continue
            candidates.append((indicator_id, meta, _signature(meta)))

        stats: dict[str, Any] = {
            "country": country,
            "ref_area_toc_files": len(country_ref),
            "ref_area_ids": sorted(str(row.get("id") or "") for row in country_ref),
            "ref_area_signature": country_signature,
            "ref_area_unchanged_gate": False,
            "indicator_toc_requested": True,
            "toc_indicators": len(toc),
            "selected_indicators": len(candidates),
            "unchanged": 0,
            "network_queries": 0,
            "replayed_from_local_cache": 0,
            "with_country_rows": 0,
            "without_country_rows": 0,
            "failed": 0,
            "business_rows": 0,
            "failures": [],
            "ref_toc_sha256": ref_snapshot.get("sha256"),
            "toc_sha256": toc_snapshot.get("sha256"),
            "data_api": base_url,
        }

        for meta in toc:
            row = dict(meta)
            row["__ivoiredata_source_url"] = toc_response.url
            row["__ivoiredata_country"] = country
            yield dlt.mark.with_table_name(row, "ilostat_indicator_catalog")

        for indicator_id, meta, signature in candidates:
            artifact = f"indicator:{indicator_id}"
            if loaded_signatures.get(indicator_id) == signature:
                stats["unchanged"] += 1
                if upstream:
                    upstream.mark_unchanged("civ_ilostat", artifact, signature=signature, url=base_url)
                continue

            cached = upstream.cached_path("civ_ilostat", artifact, signature) if upstream else None
            response_url = base_url
            raw_content: bytes | None = None
            rows: list[dict[str, str]]
            if cached is not None:
                raw_content = cached.read_bytes()
                rows = _csv_rows(raw_content)
                rows = [row for row in rows if not row.get("ref_area") or row.get("ref_area") == country]
                stats["replayed_from_local_cache"] += 1
            else:
                try:
                    response, rows = _fetch_indicator(session, indicator_id, country, data_api=base_url)
                    response_url = response.url
                    raw_content = response.content
                    stats["network_queries"] += 1
                    if pause:
                        time.sleep(pause)
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    stats["failed"] += 1
                    stats["failures"].append({"indicator_id": indicator_id, "status": status, "error": str(exc)[:1000]})
                    if upstream:
                        upstream.mark_error("civ_ilostat", artifact, url=base_url, error=str(exc), status_code=status, method="REST_INDICATOR")
                    continue

            snapshot = save_snapshot(
                snapshot_dir,
                source_id="civ_ilostat",
                url=response_url,
                content=raw_content or b"",
                content_type="text/csv",
                name=f"{indicator_id}-{country}.csv",
            )
            if rows:
                stats["with_country_rows"] += 1
                stats["business_rows"] += len(rows)
            else:
                stats["without_country_rows"] += 1

            table = _table_name(indicator_id)
            for out in rows:
                item = dict(out)
                item["__ivoiredata_source_url"] = response_url
                item["__ivoiredata_raw_sha256"] = snapshot["sha256"]
                item["__ivoiredata_raw_path"] = snapshot.get("local_path")
                item["__ivoiredata_ref_area"] = country
                item["__ivoiredata_indicator_id"] = indicator_id
                item["__ivoiredata_indicator_last_update"] = meta.get("last.update")
                yield dlt.mark.with_table_name(item, table)

            loaded_signatures[indicator_id] = signature
            if upstream:
                upstream.mark_downloaded(
                    "civ_ilostat", artifact,
                    url=response_url,
                    signature=signature,
                    sha256=str(snapshot["sha256"]),
                    size_bytes=int(snapshot["size_bytes"]),
                    method="REST_INDICATOR",
                    rows=len(rows),
                    local_path=str(snapshot.get("local_path") or "") or None,
                    extra={"last_update": meta.get("last.update"), "frequency": meta.get("freq")},
                )

        if stats["failed"] and stats["with_country_rows"] == 0 and stats["unchanged"] == 0:
            raise RuntimeError(f"ILOSTAT failed for all changed indicators: {stats['failures'][:3]}")

        # Only certify the country-wide version once the changed indicator sweep completed
        # without partial failures. A failed indicator therefore forces a retry next run.
        if stats["failed"] == 0:
            dlt_state["country_ref_area_signature_v083"] = country_signature
            if upstream:
                upstream.mark_downloaded(
                    "civ_ilostat", "ref_area:CIV", url=ref_response.url,
                    signature=country_signature, sha256=str(ref_snapshot.get("sha256") or "") or None,
                    size_bytes=int(ref_snapshot.get("size_bytes") or 0), method="ILOSTAT_REF_AREA_TOC",
                    rows=sum(int(row.get("n.records") or 0) for row in country_ref if str(row.get("n.records") or "").isdigit()),
                    local_path=str(ref_snapshot.get("local_path") or "") or None,
                    extra={"ref_area_ids": stats["ref_area_ids"]},
                )
        if snapshot_dir:
            atomic_write_json(snapshot_dir / "ilostat_sync_stats.json", stats)
        yield dlt.mark.with_table_name({"run_stats_json": json.dumps(stats, ensure_ascii=False), **stats}, "ilostat_sync_stats")

    return resource()
