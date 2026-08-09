from __future__ import annotations

from itertools import islice
from pathlib import Path
from typing import Any, Iterable

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


def world_bank_wdi_resource(
    *,
    country: str = "CIV",
    source: int = 2,
    indicator_limit: int | None = None,
    batch_size: int = 60,
    user_agent: str = "IvoireData/0.6",
    snapshot_dir: Path | None = None,
):
    import dlt
    import requests

    batch_size = max(1, min(int(batch_size), 60))

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
        for indicator in indicators:
            row = dict(indicator)
            row["__ivoiredata_source_url"] = f"{API}/indicator/{row.get('id', '')}"
            row["__ivoiredata_country"] = country
            yield dlt.mark.with_table_name(row, "worldbank_wdi_indicators")
        for batch_index, codes_batch in enumerate(_chunks(codes, batch_size)):
            joined = ";".join(codes_batch)
            rows = _paged(
                session,
                f"{API}/country/{country}/indicator/{joined}",
                {"format": "json", "per_page": 20000, "source": source},
                snapshot_dir=snapshot_dir,
                snapshot_name=f"wdi-batch-{batch_index:04d}",
            )
            for row in rows:
                item = dict(row)
                item["__ivoiredata_country"] = country
                item["__ivoiredata_source_url"] = f"{API}/country/{country}/indicator/{joined}?source={source}"
                yield dlt.mark.with_table_name(item, "worldbank_wdi")

    return resource()
