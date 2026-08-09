from pathlib import Path

from ivoiredata.connectors.bulk_catalog import _Links, _table_name
from ivoiredata.connectors.public_web import _same_host_links
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


def test_metadata_only_filters_microdata_download_links():
    links = ["/catalog/34", "/catalog/34/data-dictionary", "/catalog/34/get-microdata", "/files/data.sav", "https://other.example/file"]
    result = _same_host_links("https://nada.example/catalog", links, metadata_only=True)
    assert "https://nada.example/catalog/34" in result
    assert "https://nada.example/catalog/34/data-dictionary" in result
    assert all("microdata" not in link for link in result)
    assert all(not link.endswith(".sav") for link in result)
    assert all("other.example" not in link for link in result)


def test_bulk_link_parser_and_table_isolation():
    parser = _Links()
    parser.feed('<a href="file.csv">CSV data</a><a href="/doc">Documentation</a>')
    assert parser.links == [("file.csv", "CSV data"), ("/doc", "Documentation")]
    assert _table_name("civ_faostat") == "bulk_catalog_civ_faostat"
    assert _table_name("civ_uis") == "bulk_catalog_civ_uis"


def test_local_settings_create_file_uri(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "lake")
    settings.configure_dlt_env()
    assert settings.data_dir.exists()


def test_runtime_connectors_are_declared():
    allowed = {"data_gouv_ci", "world_bank_wdi", "geoboundaries", "ilostat_ref_area", "osm_geofabrik", "bulk_catalog", "public_web", "http_file"}
    assert {"ilostat_ref_area", "osm_geofabrik", "bulk_catalog"} <= allowed
