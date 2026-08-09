from datetime import datetime, timedelta, timezone

from ivoiredata.cleaning import clean_text
from ivoiredata.corpus import build_from_rows, row_to_training_text
from ivoiredata.crosscheck import reconcile_claims
from ivoiredata.dedup import ExactDeduplicator
from ivoiredata.freshness import FreshnessStore
from ivoiredata.models import SourceSpec
from ivoiredata.provenance import Claim
from ivoiredata.quality import score_text
from ivoiredata.ranking import source_score
from ivoiredata.registry import infer_connector
from ivoiredata.connectors.public_web import _same_host_links
from ivoiredata.connectors.world_bank import _chunks, _data_rows


def spec(**kwargs):
    data = dict(source_id="x", title="X", domain="test", provider="P", source_url="https://example.com/a", rights_tier="A_REDISTRIBUTABLE", access_tier="OPEN", priority="P0")
    data.update(kwargs)
    return SourceSpec(**data)


def test_connector_inference():
    assert infer_connector(spec(source_id="civ_datagouv_catalog", source_url="https://data.gouv.ci/datasets")) == "data_gouv_ci"
    assert infer_connector(spec(source_url="https://example.com/data.csv")) == "http_file"
    assert infer_connector(spec(source_url="https://example.com/page")) == "public_web"


def test_public_access_tiers():
    assert spec(access_tier="OPEN").public
    assert spec(access_tier="OPEN_PUBLIC", rights_tier="C_PUBLIC_LOCAL_INGEST").public
    assert not spec(access_tier="MIXED", rights_tier="C_DATASET_SPECIFIC").public
    assert not spec(access_tier="OPEN_METADATA_DOWNLOAD_CONTROLLED", rights_tier="D_RESEARCH_OR_DATASET_TERMS").public


def test_same_host_crawler_filter():
    links = _same_host_links("https://example.com/start", ["/a", "https://example.com/b.pdf", "https://other.example/x", "/map.osm.pbf", "mailto:x@example.com"])
    assert "https://example.com/a" in links
    assert "https://example.com/b.pdf" in links
    assert not any("other.example" in value for value in links)
    assert not any(value.endswith(".pbf") for value in links)


def test_world_bank_helpers():
    assert list(_chunks(["a", "b", "c"], 2)) == [["a", "b"], ["c"]]
    meta, rows = _data_rows([{"page": 1, "pages": 1}, [{"id": "x"}]])
    assert meta["page"] == 1
    assert rows == [{"id": "x"}]


def test_freshness(tmp_path):
    store = FreshnessStore(tmp_path / "state.json")
    source = spec(refresh_hours=24)
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    assert store.due(source, now)
    store.mark("x", success=True, now=now)
    assert not store.due(source, now + timedelta(hours=23))
    assert store.due(source, now + timedelta(hours=25))


def test_clean_dedup_quality():
    text = "Bonjour\x00   Côte d’Ivoire. " * 40
    cleaned = clean_text(text)
    assert "\x00" not in cleaned
    assert score_text(cleaned) > 0.35
    dedup = ExactDeduplicator()
    assert dedup.accept(cleaned)[0]
    assert not dedup.accept(cleaned)[0]


def test_corpus_builder(tmp_path):
    rows = [{"text": "La Côte d’Ivoire possède une économie diversifiée et des données publiques vérifiables. " * 20, "source_id": "a"}, {"text": "La Côte d’Ivoire possède une économie diversifiée et des données publiques vérifiables. " * 20, "source_id": "a"}, {"population": 29389150, "annee": 2021, "__ivoiredata_source_url": "https://example.com"}]
    stats = build_from_rows(rows, tmp_path, "v1", shard_size=1, min_quality=0.1)
    assert stats.documents_seen == 3
    assert stats.duplicates == 1
    assert stats.documents_written >= 1
    assert (tmp_path / "v1" / "manifest.json").exists()
    assert "population" in row_to_training_text(rows[2])


def test_ranking_and_crosscheck():
    source = spec(provider="ANStat", priority="P0", domain="demography")
    assert source_score(source, domain="demography") >= 0.9
    result = reconcile_claims([Claim(10, "official-a", "https://a", authority=1.0), Claim(10, "official-b", "https://b", authority=0.9), Claim(11, "secondary", "https://c", authority=0.3)])
    assert result["status"] == "conflict"
    assert result["value"] == 10
    assert result["confidence"] > 0.8
