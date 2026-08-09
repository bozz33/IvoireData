from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Iterable


def ilostat_ref_area_resource(*, country: str = "CIV", frequencies: Iterable[str] = ("A",), base_url: str = "https://rplumber.ilo.org/files/ref_area", user_agent: str = "IvoireData/0.5"):
    """Load ILOSTAT country datasets from the official bulk backend.

    ILOSTAT publishes country/frequency extracts. The official Rilostat client
    consumes RDS files from the same backend; pyreadr is used here so IvoireData
    can normalize them with dlt.
    """
    import dlt
    import pyreadr
    import requests

    @dlt.resource(name="ilostat_civ", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent})
        for frequency in frequencies:
            freq = str(frequency).upper().strip()
            url = f"{base_url.rstrip('/')}/{country.upper()}_{freq}.rds"
            response = session.get(url, timeout=240)
            response.raise_for_status()
            digest = hashlib.sha256(response.content).hexdigest()
            with tempfile.NamedTemporaryFile(suffix=".rds", delete=False) as tmp:
                tmp.write(response.content)
                tmp_path = Path(tmp.name)
            try:
                frames = pyreadr.read_r(str(tmp_path))
                for _, frame in frames.items():
                    if frame is None:
                        continue
                    for row in frame.to_dict(orient="records"):
                        row["__ivoiredata_source_url"] = url
                        row["__ivoiredata_raw_sha256"] = digest
                        row["__ivoiredata_frequency"] = freq
                        yield dlt.mark.with_table_name(row, f"ilostat_{country.lower()}_{freq.lower()}")
            finally:
                tmp_path.unlink(missing_ok=True)

    return resource()
