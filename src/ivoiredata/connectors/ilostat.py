from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Iterable

from ..snapshots import save_snapshot

# Official country-filterable CSV backend. `obs_status` is an observation status/flag,
# not a frequency. It must be preserved and never used to discard rows.
CSV_BASE = "https://rplumber.ilo.org/data/indicator"


class RdsParseError(RuntimeError):
    """Legacy compatibility error for the retired RDS path."""


def _read_rds_rows(path: Path):
    raise RdsParseError(f"RDS parsing disabled, use CSV backend ({path.name})")


def _csv_rows(content: bytes) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig", "replace")))
    return [{str(k): v for k, v in row.items() if k is not None} for row in reader]


def ilostat_ref_area_resource(
    *,
    country: str = "CIV",
    frequencies: Iterable[str] = (),
    base_url: str = CSV_BASE,
    user_agent: str = "IvoireData/0.7",
    snapshot_dir: Path | None = None,
):
    """Load ILOSTAT country indicator data from the CSV backend.

    `frequencies` is retained only for backward compatibility with older runtime configs.
    The `/data/indicator` response used here is already the selected indicator dataset;
    `obs_status` is preserved verbatim because it describes observation status (for
    example normal/revised), not periodicity.
    """
    import dlt
    import requests

    @dlt.resource(name="ilostat_civ", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent, "Accept": "text/csv"})
        response = session.get(base_url.rstrip("/"), params={"ref_area": country}, timeout=240)
        response.raise_for_status()
        snapshot = save_snapshot(
            snapshot_dir,
            source_id="civ_ilostat",
            url=response.url,
            content=response.content,
            content_type=response.headers.get("content-type"),
            name=f"{country}_indicator.csv",
        )
        digest = str(snapshot["sha256"])
        raw_path = snapshot.get("local_path")
        rows = _csv_rows(response.content)
        if not rows:
            raise RuntimeError(f"ILOSTAT returned no rows for ref_area={country}")
        for out in rows:
            out["__ivoiredata_source_url"] = response.url
            out["__ivoiredata_raw_sha256"] = digest
            out["__ivoiredata_raw_path"] = raw_path
            out["__ivoiredata_ref_area"] = country
            yield dlt.mark.with_table_name(out, f"ilostat_{country.lower()}")

    return resource()
