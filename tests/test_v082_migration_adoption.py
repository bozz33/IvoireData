from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ivoiredata.connectors.osm_geofabrik import geofabrik_snapshot_resource
from ivoiredata.connectors.world_bank import _v081_wdi_adoptable


class FakeResponse:
    def __init__(self, *, text: str = "", status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.headers = {}
        self.content = text.encode()

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Md5OnlySession:
    def __init__(self, expected_md5: str):
        self.expected_md5 = expected_md5
        self.headers = {}
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url.endswith(".md5"):
            return FakeResponse(text=f"{self.expected_md5}  ivory-coast-latest.osm.pbf\n")
        raise AssertionError(f"PBF body must not be requested during adoption: {url}")


def test_geofabrik_adopts_existing_matching_pbf_without_body_download(tmp_path: Path, monkeypatch):
    import requests

    output = tmp_path / "raw"
    output.mkdir()
    pbf = output / "ivory-coast-latest.osm.pbf"
    content = b"existing-osm-pbf-content"
    pbf.write_bytes(content)
    md5 = hashlib.md5(content).hexdigest()
    fake = Md5OnlySession(md5)
    monkeypatch.setattr(requests, "Session", lambda: fake)

    resource = geofabrik_snapshot_resource(
        page_url="https://download.geofabrik.de/africa/ivory-coast.html",
        output_dir=output,
        upstream_state_path=tmp_path / "state" / "upstreams.json",
    )
    rows = list(resource)
    assert len(rows) == 1
    assert rows[0]["changed"] is False
    assert rows[0]["adopted_existing"] is True
    assert rows[0]["md5"] == md5
    assert fake.calls == ["https://download.geofabrik.de/africa/ivory-coast-latest.osm.pbf.md5"]


def test_wdi_adoption_requires_success_manifest_table_and_fresh_country_snapshot(tmp_path: Path):
    source_root = tmp_path / "civ_worldbank_wdi"
    raw = source_root / "raw"
    raw.mkdir(parents=True)
    table = source_root / "tables" / "data" / "worldbank_wdi"
    table.mkdir(parents=True)
    (table / "existing.parquet").write_bytes(b"placeholder")
    (source_root / "manifest.json").write_text(json.dumps({"status": "success"}), encoding="utf-8")
    (raw / "batch.json.meta.json").write_text(json.dumps({
        "source_url": "https://api.worldbank.org/v2/country/CIV/indicator/SP.POP.TOTL?source=2",
        "retrieved_at": "2026-08-10T20:00:00Z",
    }), encoding="utf-8")

    ok, stamp = _v081_wdi_adoptable(raw, country="CIV", lastupdated="2026-08-09T00:00:00Z")
    assert ok is True
    assert stamp == "2026-08-10T20:00:00Z"

    too_old, _ = _v081_wdi_adoptable(raw, country="CIV", lastupdated="2026-08-11T00:00:00Z")
    assert too_old is False

    (source_root / "manifest.json").write_text(json.dumps({"status": "error"}), encoding="utf-8")
    bad_manifest, _ = _v081_wdi_adoptable(raw, country="CIV", lastupdated="2026-08-09T00:00:00Z")
    assert bad_manifest is False
