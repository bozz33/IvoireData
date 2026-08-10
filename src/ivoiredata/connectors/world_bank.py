from __future__ import annotations

from itertools import islice
from pathlib import Path
from typing import Any, Iterable

from ..metadata import classify_from_base
from ..snapshots import save_snapshot

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


def _paged(session, url: str, params: dict[str, Any], *, snapshot_dir: Path | None = None, source_id: str = "civ_worldbank_wdi", snapshot_name: str = "page") -> list[dict[str, Any]]:
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


def _fetch_country_indicators(session, country: str, codes: list[str], source: int, *, snapshot_dir, snapshot_name) -> list[dict[str, Any]]:
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
                out.extend(_fetch_country_indicators(session, country, half, source, snapshot_dir=snapshot_dir, snapshot_name=f"{snapshot_name}-h{half_index}"))
            except requests.exceptions.HTTPError as exc2:
                if getattr(getattr(exc2, "response", None), "status_code", None) == 400 and len(half) == 1:
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


def world_bank_wdi_resource(
    *,
    country: str = "CIV",
    source: int = 2,
    indicator_limit: int | None = None,
    batch_size: int = 60,
    user_agent: str = "IvoireData/0.8",
    snapshot_dir: Path | None = None,
    metadata_base: dict[str, Any] | None = None,
):
    import dlt
    import requests

    batch_size = max(1, min(int(batch_size), 60))
    base = dict(metadata_base or {})

    @dlt.resource(name="world_bank_wdi", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
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
        for batch_index, codes_batch in enumerate(_chunks(codes, batch_size)):
            joined = ";".join(codes_batch)
            rows = _fetch_country_indicators(
                session, country, codes_batch, source,
                snapshot_dir=snapshot_dir,
                snapshot_name=f"wdi-batch-{batch_index:04d}",
            )
            for row in rows:
                item = dict(row)
                indicator = item.get("indicator") if isinstance(item.get("indicator"), dict) else {}
                code = str(indicator.get("id") or "")
                item["__ivoiredata_country"] = country
                item["__ivoiredata_source_url"] = f"{API}/country/{country}/indicator/{code or joined}?source={source}"
                item.update(classifications.get(code, {}))
                yield dlt.mark.with_table_name(item, "worldbank_wdi")

    return resource()
