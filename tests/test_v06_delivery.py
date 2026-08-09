import json
from pathlib import Path

from ivoiredata.delivery import ensure_source_layout, rebuild_catalog, source_paths, write_source_manifest
from ivoiredata.models import SourceSpec
from ivoiredata.settings import Settings
from ivoiredata.snapshots import save_snapshot


def spec(domain="agriculture", source_id="civ_test"):
    return SourceSpec(
        source_id=source_id,
        title="Test Source",
        domain=domain,
        provider="Provider",
        source_url="https://example.org/data",
        rights_tier="A_REDISTRIBUTABLE",
        access_tier="OPEN",
        priority="P0",
        connector="http_file",
        refresh_hours=24,
        auto_sync=True,
    )


def test_domain_source_layout(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "lake")
    s = spec()
    paths = ensure_source_layout(settings, s)
    assert paths["root"] == settings.data_dir / "domains" / "agriculture" / "civ_test"
    assert paths["raw"].is_dir()
    assert paths["tables"].is_dir()
    assert paths["documents"].is_dir()


def test_snapshot_is_content_addressed(tmp_path: Path):
    out = save_snapshot(tmp_path, source_id="civ_test", url="https://example.org/file.csv", content=b"a,b\n1,2\n", content_type="text/csv")
    path = Path(out["local_path"])
    assert path.exists()
    assert path.with_suffix(path.suffix + ".meta.json").exists()
    assert len(out["sha256"]) == 64


def test_manifest_and_global_catalog(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "lake")
    s = spec()
    ensure_source_layout(settings, s)
    manifest = write_source_manifest(settings, s, status="success", connector="http_file", started_at="2026-08-09T00:00:00Z", finished_at="2026-08-09T00:01:00Z")
    assert manifest["source_id"] == "civ_test"
    assert manifest["domain"] == "agriculture"
    catalog = rebuild_catalog(settings, [s])
    assert catalog["domains"]["agriculture"][0]["source_id"] == "civ_test"
    stored = json.loads((settings.data_dir / "catalog.json").read_text(encoding="utf-8"))
    assert stored["sources"][0]["status"] == "success"


def test_source_paths_are_stable(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "lake")
    a = source_paths(settings, spec(domain="law_justice", source_id="civ_justice"))
    b = source_paths(settings, spec(domain="law_justice", source_id="civ_justice"))
    assert a == b
    assert "law_justice" in str(a["root"])
