from __future__ import annotations

import hashlib
import json
from itertools import islice
from pathlib import Path
from typing import Any, Iterable

from ..metadata import classify_from_base
from ..snapshots import save_snapshot
from ..state_io import atomic_write_json
from ..upstream_state import UpstreamState

API = "https://api.worldbank.org/v2"


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    iterator = iter(items)
    while True:
        chunk = list(islice(iterator, size))
        if not chunk:
            return
        yield chunk


def _data_rows(payload: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if isinstance(payload, list) and len(payload) >= 2:
        meta = payload[0] if isinstance(payload[0], dict) else {}
        rows = payload[1] if isinstance(payload[1], list) else []
        return meta, [row for row in rows if isinstance(row, dict)]
    return {}, []


def _paged(session, url: str, params: dict[str, Any], *, snapshot_dir: Path | None = None,
           source_id: str = "civ_worldbank_wdi", snapshot_name: str = "page") -> list[dict[str, Any]]:
    page = 1
    rows: list[dict[str, Any]] = []
    while True:
        query = dict(params)
        query["page"] = page
        response = session.get(url, params=query, timeout=180)
        response.raise_for_status()
        save_snapshot(
            snapshot_dir,
            source_id=source_id,
            url=response.url,
            content=response.content,
            content_type=response.headers.get("content-type"),
            name=f"{snapshot_name}-{page}.json",
        )
        meta, batch = _data_rows(response.json())
        rows.extend(batch)
        pages = int(meta.get("pages") or 1)
        if page >= pages:
            return rows
        page += 1


def _fetch_country_indicators(session, country: str, codes: list[str], source: int, *, snapshot_dir,
                              snapshot_name, ignored_codes: list[str]) -> list[dict[str, Any]]:
    import requests

    joined = ";".join(codes)
    try:
        return _paged(
            session,
            f"{API}/country/{country}/indicator/{joined}",
            {"format": "json", "per_page": 20000, "source": source},
            snapshot_dir=snapshot_dir,
            snapshot_name=snapshot_name,
        )
    except requests.exceptions.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status != 400 or len(codes) <= 1:
            raise
        mid = len(codes) // 2
        out: list[dict[str, Any]] = []
        for half_index, half in enumerate((codes[:mid], codes[mid:])):
            try:
                out.extend(_fetch_country_indicators(
                    session, country, half, source,
                    snapshot_dir=snapshot_dir,
                    snapshot_name=f"{snapshot_name}-h{half_index}",
                    ignored_codes=ignored_codes,
                ))
            except requests.exceptions.HTTPError as exc2:
                if getattr(getattr(exc2, "response", None), "status_code", None) == 400 and len(half) == 1:
                    ignored_codes.append(half[0])
                    print(f"[world_bank_wdi] indicateur ignoré (400) : {half[0]}", flush=True)
                    continue
                raise
        return out


def _indicator_classification(indicator: dict[str, Any], metadata_base: dict[str, Any]) -> dict[str, Any]:
    code = str(indicator.get("id") or "")
    text = " ".join(str(indicator.get(key) or "") for key in ("name", "sourceNote", "sourceOrganization", "topics"))
    classified = classify_from_base(metadata_base, f"{API}/indicator/{code}", text, document_type="DATASET")
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


def _source_version(session, source: int, snapshot_dir: Path | None) -> tuple[str, dict[str, Any], str]:
    """Return the official World Bank `lastupdated` source signature."""
    url = f"{API}/sources/{source}"
    response = session.get(url, params={"format": "json", "per_page": 100}, timeout=120)
    response.raise_for_status()
    save_snapshot(
        snapshot_dir,
        source_id="civ_worldbank_wdi",
        url=response.url,
        content=response.content,
        content_type=response.headers.get("content-type"),
        name=f"source-{source}-metadata.json",
    )
    _, rows = _data_rows(response.json())
    row = rows[0] if rows else {}
    lastupdated = str(row.get("lastupdated") or row.get("lastUpdated") or "").strip()
    signature_payload = {
        "source": source,
        "lastupdated": lastupdated,
        "name": row.get("name"),
        "code": row.get("code"),
        "dataavailability": row.get("dataavailability"),
    }
    signature = hashlib.sha256(json.dumps(signature_payload, sort_keys=True, default=str).encode()).hexdigest()
    return signature, row, response.url


def world_bank_wdi_resource(
    *,
    country: str = "CIV",
    source: int = 2,
    indicator_limit: int | None = None,
    batch_size: int = 60,
    user_agent: str = "IvoireData/0.8.2",
    snapshot_dir: Path | None = None,
    metadata_base: dict[str, Any] | None = None,
    upstream_state_path: Path | None = None,
):
    import dlt
    import requests

    batch_size = max(1, min(int(batch_size), 60))
    base = dict(metadata_base or {})

    @dlt.resource(name="world_bank_wdi", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        loaded = dlt.current.resource_state()
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None
        signature, source_meta, source_meta_url = _source_version(session, source, snapshot_dir)
        prior_signature = loaded.get("source_signature")

        stats: dict[str, Any] = {
            "country": country,
            "source": source,
            "source_lastupdated": source_meta.get("lastupdated") or source_meta.get("lastUpdated"),
            "unchanged": prior_signature == signature,
            "indicators": 0,
            "business_rows": 0,
            "ignored_http400_indicators": [],
        }

        if prior_signature == signature:
            if upstream:
                upstream.mark_unchanged("civ_worldbank_wdi", f"source:{source}", signature=signature, url=source_meta_url, reason="WORLD_BANK_LASTUPDATED")
            if snapshot_dir:
                atomic_write_json(snapshot_dir / "worldbank_wdi_sync_stats.json", stats)
            yield dlt.mark.with_table_name({"run_stats_json": json.dumps(stats, ensure_ascii=False), **stats}, "worldbank_wdi_sync_stats")
            return

        indicators = _paged(
            session,
            f"{API}/indicator",
            {"format": "json", "per_page": 20000, "source": source},
            snapshot_dir=snapshot_dir,
            snapshot_name="indicators",
        )
        if indicator_limit is not None:
            indicators = indicators[: max(0, int(indicator_limit))]
        codes = [str(row.get("id")) for row in indicators if row.get("id")]
        stats["indicators"] = len(codes)
        classifications: dict[str, dict[str, Any]] = {}
        for indicator in indicators:
            row = dict(indicator)
            code = str(row.get("id") or "")
            classified = _indicator_classification(row, base)
            classifications[code] = classified
            row["__ivoiredata_source_url"] = f"{API}/indicator/{code}"
            row["__ivoiredata_country"] = country
            row.update(classified)
            yield dlt.mark.with_table_name(row, "worldbank_wdi_indicators")

        ignored_codes: list[str] = []
        business_rows = 0
        for batch_index, codes_batch in enumerate(_chunks(codes, batch_size)):
            joined = ";".join(codes_batch)
            rows = _fetch_country_indicators(
                session, country, codes_batch, source,
                snapshot_dir=snapshot_dir,
                snapshot_name=f"wdi-batch-{batch_index:04d}",
                ignored_codes=ignored_codes,
            )
            for row in rows:
                item = dict(row)
                indicator = item.get("indicator") if isinstance(item.get("indicator"), dict) else {}
                code = str(indicator.get("id") or "")
                item["__ivoiredata_country"] = country
                item["__ivoiredata_source_url"] = f"{API}/country/{country}/indicator/{code or joined}?source={source}"
                item.update(classifications.get(code, {}))
                business_rows += 1
                yield dlt.mark.with_table_name(item, "worldbank_wdi")

        loaded["source_signature"] = signature
        stats["business_rows"] = business_rows
        stats["ignored_http400_indicators"] = sorted(set(ignored_codes))
        if upstream:
            upstream.mark_downloaded(
                "civ_worldbank_wdi", f"source:{source}",
                url=source_meta_url, signature=signature, sha256=None, size_bytes=None,
                method="WORLD_BANK_LASTUPDATED", rows=business_rows,
                extra={"lastupdated": stats["source_lastupdated"], "ignored_indicators": stats["ignored_http400_indicators"]},
            )
        if snapshot_dir:
            atomic_write_json(snapshot_dir / "worldbank_wdi_sync_stats.json", stats)
        yield dlt.mark.with_table_name({"run_stats_json": json.dumps(stats, ensure_ascii=False), **stats}, "worldbank_wdi_sync_stats")

    return resource()
