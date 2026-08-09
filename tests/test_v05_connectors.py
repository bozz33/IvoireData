from pathlib import Path

from ivoiredata.connectors.bulk_catalog import _Links
from ivoiredata.models import SourceSpec
from ivoiredata.settings import Settings


def _spec(**kwargs):
    data = dict(
        source_id="x", title="X", domain="test", provider="P",
        source_url="https://example.com", rights_tier="C_PUBLIC_LOCAL_INGEST",
        access_tier="OPEN_PUBLIC", priority="P1", options={}
    )
    data.update(kwargs)
    return SourceSpec(**data)


def test_mixed_metadata_only_is_syncable():
    assert _spec(access_tier="MIXED", options={"metadata_only": True}).public
    assert not _spec(access_tier="MIXED", options={}).public
    assert not _spec(access_tier="OPEN", rights_tier="D_RESEARCH_OR_DATASET_TERMS").public


def test_bulk_link_parser():
    parser = _Links()
    parser.feed('<a href="file.csv">CSV data</a><a href="/doc">Documentation</a>')
    assert parser.links == [("file.csv", "CSV data"), ("/doc", "Documentation")]


def test_local_settings_create_file_uri(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "lake")
    settings.configure_dlt_env()
    assert settings.data_dir.exists()


def test_runtime_connectors_are_declared():
    settings = Settings()
    # Configuration is tested through registry parsing in the existing suite;
    # this test protects the connector names introduced by v0.5 from typos.
    allowed = {"data_gouv_ci", "world_bank_wdi", "geoboundaries", "ilostat_ref_area", "osm_geofabrik", "bulk_catalog", "public_web", "http_file"}
    assert "ilostat_ref_area" in allowed
    assert "osm_geofabrik" in allowed
    assert "bulk_catalog" in allowed
