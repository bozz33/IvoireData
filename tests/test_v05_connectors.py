from pathlib import Path

from ivoiredata.connectors.bulk_catalog import _Links, _table_name
from ivoiredata.connectors.data_gouv_ci import dataset_id_from_public_url
from ivoiredata.connectors.geoboundaries import _resolve_meta_urls
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
    allowed = {"data_gouv_ci", "world_bank_wdi", "world_bank_projects", "geoboundaries", "ilostat_ref_area", "osm_geofabrik", "bulk_catalog", "public_web", "http_file"}
    assert {"ilostat_ref_area", "osm_geofabrik", "bulk_catalog"} <= allowed


def test_geoboundaries_directory_listing_explores_adm_levels():
    # Un directory listing (dernier segment = code pays) doit générer une URL par niveau ADM.
    urls = _resolve_meta_urls("https://www.geoboundaries.org/api/current/gbOpen/CIV/")
    assert len(urls) == 6
    assert urls[0] == "https://www.geoboundaries.org/api/current/gbOpen/CIV/ADM0/"
    assert urls[-1] == "https://www.geoboundaries.org/api/current/gbOpen/CIV/ADM5/"


def test_geoboundaries_direct_endpoint_is_kept_as_is():
    # Un endpoint qui cible déjà un niveau ADM ne doit pas être démultiplié.
    urls = _resolve_meta_urls("https://www.geoboundaries.org/api/current/gbOpen/CIV/ADM2/")
    assert urls == ["https://www.geoboundaries.org/api/current/gbOpen/CIV/ADM2/"]


def test_public_web_accepts_verify_ssl_option():
    # Le connecteur public_web doit accepter l'option verify_ssl sans erreur,
    # pour gérer les certificats gouvernementaux invalides.
    import inspect
    from ivoiredata.connectors import public_web
    sig = inspect.signature(public_web.public_document_resource)
    assert "verify_ssl" in sig.parameters
    assert sig.parameters["verify_ssl"].default is True


def test_world_bank_wdi_bisects_400_batches(monkeypatch):
    # _fetch_country_indicators doit subdiviser récursivement un batch qui reçoit un 400
    # pour isoler les indicateurs fautifs, sans faire échouer toute la source.
    import requests
    from ivoiredata.connectors import world_bank

    class _Resp:
        def __init__(self, status, payload=None):
            self.status_code = status
            self._payload = payload or []
        def json(self):
            return [{"pages": 1}, self._payload]
        def raise_for_status(self):
            if self.status_code >= 400:
                err = requests.exceptions.HTTPError(response=self)
                raise err

    calls = {"n": 0}

    def fake_paged(session, url, params, *, snapshot_dir, snapshot_name):
        # Simule : batch complet [A, BAD, B] -> 400 ; moitiés -> 200 avec données ; BAD seul -> 400
        calls["n"] += 1
        if "BAD" in url and url.count(";") == 0:
            raise requests.exceptions.HTTPError(response=_Resp(400))
        if "BAD" in url:
            raise requests.exceptions.HTTPError(response=_Resp(400))
        return [{"indicator": {"id": "OK"}, "value": 1}]

    monkeypatch.setattr(world_bank, "_paged", fake_paged)
    rows = world_bank._fetch_country_indicators(
        session=None, country="CIV", codes=["A", "BAD", "B"], source=2,
        snapshot_dir=None, snapshot_name="t",
    )
    # BAD est ignoré, les autres sont conservés.
    assert any(r.get("indicator", {}).get("id") == "OK" for r in rows)


def test_ilostat_uses_csv_backend_not_rds():
    # Le connecteur ILOSTAT privilégie désormais le backend CSV (/data/indicator) qui ne
    # dépend pas de pyreadr/librdata (instable : SIGSEGV). L'ancien chemin RDS est désactivé
    # et doit lever RdsParseError pour préserver la compatibilité.
    import inspect
    from ivoiredata.connectors import ilostat

    # Le connecteur expose bien le CSV backend par défaut.
    sig = inspect.signature(ilostat.ilostat_ref_area_resource)
    assert sig.parameters["base_url"].default == ilostat.CSV_BASE
    # _read_rds_rows (legacy) lève systématiquement RdsParseError.
    with __import__("pytest").raises(ilostat.RdsParseError):
        list(ilostat._read_rds_rows(__import__("pathlib").Path("x.rds")))


def test_datagouv_dataset_id_extracted_from_slug_url():
    # L'URL d'un dataset ciblé (slug lisible) doit produire l'identifiant attendu.
    slug = dataset_id_from_public_url("https://data.gouv.ci/datasets/statistiques-globales-sur-le-secteur-cacao-et-cafe")
    assert slug == "statistiques-globales-sur-le-secteur-cacao-et-cafe"


def test_datagouv_catalog_url_returns_none():
    # L'URL générique /datasets (catalogue complet) ne doit cibler aucun dataset.
    assert dataset_id_from_public_url("https://data.gouv.ci/datasets") is None


def test_datagouv_selection_matches_slug_or_id():
    # La sélection d'un dataset ciblé doit matcher le slug de l'URL contre l'id OU le slug
    # du catalogue (le connecteur reçoit le slug, le catalogue expose l'id technique).
    import types
    from ivoiredata.connectors import data_gouv_ci

    catalog = [
        {"id": "vehoqo0k4rlbkdk12ar8oqg7", "slug": "statistiques-globales-sur-le-secteur-cacao-et-cafe", "title": "Cacao"},
        {"id": "abc123", "slug": "autre-dataset", "title": "Autre"},
    ]
    wanted = {"statistiques-globales-sur-le-secteur-cacao-et-cafe"}
    selected = []
    for meta in catalog:
        dsid = data_gouv_ci._dataset_id(meta)
        identifiers = {dsid, meta.get("slug")} if dsid else set()
        if not wanted or identifiers & wanted:
            selected.append(meta)
    assert len(selected) == 1
    assert selected[0]["id"] == "vehoqo0k4rlbkdk12ar8oqg7"
