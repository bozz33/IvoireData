from __future__ import annotations

from pathlib import Path
from typing import Any

from ..snapshots import save_snapshot

API = "https://api.uis.unesco.org/api/public"


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "items", "records", "value"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if payload and all(isinstance(value, dict) for value in payload.values()):
            return [dict(value) for value in payload.values()]
    return []


def _get_json(session, url: str, *, params: list[tuple[str, str]] | dict[str, Any] | None = None, timeout: int = 180):
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response, response.json()


def uis_country_resource(
    *,
    geo_unit: str = "CIV",
    start_year: int | None = None,
    end_year: int | None = None,
    user_agent: str = "IvoireData/0.7",
    snapshot_dir: Path | None = None,
):
    """Load UNESCO UIS indicator data for one country from the official Data API.

    The UIS API accepts an ISO3 `geoUnit` filter and returns up to 100,000 records per
    request. A Côte d'Ivoire request is comfortably below that limit. Definitions and
    the country payload are snapshotted for reproducibility.
    """
    import dlt
    import requests

    @dlt.resource(name="uis_civ", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})

        definitions_url = f"{API}/definitions/indicators"
        response, payload = _get_json(session, definitions_url)
        definition_snapshot = save_snapshot(
            snapshot_dir,
            source_id="civ_uis",
            url=response.url,
            content=response.content,
            content_type=response.headers.get("content-type"),
            name="uis-indicator-definitions.json",
        )
        for row in _rows(payload):
            item = dict(row)
            item["__ivoiredata_source_url"] = response.url
            item["__ivoiredata_raw_sha256"] = definition_snapshot["sha256"]
            item["__ivoiredata_raw_path"] = definition_snapshot.get("local_path")
            yield dlt.mark.with_table_name(item, "uis_indicators")

        geounits_url = f"{API}/definitions/geounits"
        response, payload = _get_json(session, geounits_url)
        geounit_snapshot = save_snapshot(
            snapshot_dir,
            source_id="civ_uis",
            url=response.url,
            content=response.content,
            content_type=response.headers.get("content-type"),
            name="uis-geounits.json",
        )
        for row in _rows(payload):
            code = str(
                row.get("geoUnitCode")
                or row.get("code")
                or row.get("id")
                or row.get("iso3")
                or ""
            )
            if code and code.upper() != geo_unit.upper():
                continue
            item = dict(row)
            item["__ivoiredata_source_url"] = response.url
            item["__ivoiredata_raw_sha256"] = geounit_snapshot["sha256"]
            item["__ivoiredata_raw_path"] = geounit_snapshot.get("local_path")
            yield dlt.mark.with_table_name(item, "uis_geounits")

        params: list[tuple[str, str]] = [("geoUnit", geo_unit)]
        if start_year is not None:
            params.append(("startYear", str(int(start_year))))
        if end_year is not None:
            params.append(("endYear", str(int(end_year))))
        data_url = f"{API}/data/indicators"
        response, payload = _get_json(session, data_url, params=params, timeout=240)
        data_snapshot = save_snapshot(
            snapshot_dir,
            source_id="civ_uis",
            url=response.url,
            content=response.content,
            content_type=response.headers.get("content-type"),
            name=f"uis-{geo_unit}-indicators.json",
        )
        rows = _rows(payload)
        if not rows:
            raise RuntimeError(f"UIS API returned no indicator rows for geoUnit={geo_unit}")
        for row in rows:
            item = dict(row)
            item["__ivoiredata_geo_unit"] = geo_unit
            item["__ivoiredata_source_url"] = response.url
            item["__ivoiredata_raw_sha256"] = data_snapshot["sha256"]
            item["__ivoiredata_raw_path"] = data_snapshot.get("local_path")
            yield dlt.mark.with_table_name(item, "uis_data")

    return resource()
