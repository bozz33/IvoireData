from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ivoiredata.discoveries as discoveries_module
from ivoiredata.connectors.public_web import _write_needs_ocr
from ivoiredata.delivery import rebuild_catalog, write_source_manifest
from ivoiredata.discoveries import data_gouv_discoveries
from ivoiredata.metadata import classify_from_base, infer_document_type, infer_domains, source_metadata
from ivoiredata.models import SourceSpec
from ivoiredata.qualification import QualificationStore
from ivoiredata.registry import SourceRegistry
from ivoiredata.runtime_control import load_runtime_config
from ivoiredata.settings import Settings


def _spec(**kwargs) -> SourceSpec:
    data = {
        "source_id": "civ_demo", "title": "Demo", "domain": "health", "provider": "Demo Provider",
        "source_url": "https://example.test", "rights_tier": "C_PUBLIC_LOCAL_INGEST",
        "access_tier": "OPEN_PUBLIC", "priority": "P0", "connector": "public_web",
        "refresh_hours": 72, "auto_sync": True, "enabled": True, "options": {},
    }
    data.update(kwargs); return SourceSpec(**data)


class _Result:
    def __init__(self, source_id: str, status: str): self.source_id, self.status = source_id, status


def test_source_metadata_is_explicitly_cote_divoire():
    meta = source_metadata(_spec())
    assert meta["country_code"] == "CIV"
    assert meta["country_name"] == "Côte d'Ivoire"
    assert meta["primary_domain"] == "health"
    assert meta["language"] == "fr"
    assert meta["rights_tier"] == "C_PUBLIC_LOCAL_INGEST"


def test_multidomain_classifier_uses_deterministic_keywords():
    primary, secondary, confidence = infer_domains("multidomain", "Rapport sur les hôpitaux, la santé publique et la mortalité en Côte d'Ivoire")
    assert primary == "health" and confidence > 0.6 and isinstance(secondary, list)


def test_expanded_taxonomy_classifies_gender_digital_and_investment():
    assert infer_domains("multidomain", "Politique nationale sur le genre et les droits des femmes")[0] == "gender"
    assert infer_domains("multidomain", "Transformation numérique, cybersécurité et services digitaux")[0] in {"digital", "cybersecurity_public_policy"}
    assert infer_domains("multidomain", "Promotion des investissements et agréments CEPICI")[0] == "investment"


def test_document_type_classifier_detects_budget_and_law():
    assert infer_document_type("https://example.ci/doc", "Budget citoyen et loi de finances 2026") in {"LAW", "BUDGET"}
    assert infer_document_type("https://example.ci/decret-2026", "Décret portant organisation") == "DECREE"


def test_classify_from_base_preserves_country_and_rights():
    base = source_metadata(_spec(domain="multidomain", options={"language": "fr"}))
    row = classify_from_base(base, "https://example.ci/elections", "Résultats officiels de l'élection")
    assert row["country_code"] == "CIV"
    assert row["rights_tier"] == "C_PUBLIC_LOCAL_INGEST"
    assert row["primary_domain"] == "elections"
    assert row["retrieved_at"].endswith("Z")


def test_manifest_v3_and_catalog_are_country_aware(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "lake", state_dir=tmp_path / "state", ci_gold_runtime_path=tmp_path / "ci_gold.json", ci_coverage_path=tmp_path / "coverage.json", ci_gold_registry_path=tmp_path / "extra.csv")
    spec = _spec()
    manifest = write_source_manifest(settings, spec, status="success", connector=spec.connector, started_at="2026-08-10T00:00:00Z", finished_at="2026-08-10T00:01:00Z")
    assert manifest["schema_version"] == 3 and manifest["country_code"] == "CIV"
    catalog = rebuild_catalog(settings, [spec])
    assert catalog["schema_version"] == 3 and catalog["country_code"] == "CIV"
    assert catalog["domain_index"]["health"] == ["civ_demo"]


def test_packaged_overlay_precedes_local_override(tmp_path: Path):
    base, overlay, local = tmp_path / "base.json", tmp_path / "overlay.json", tmp_path / "local.json"
    base.write_text(json.dumps({"sources": {"demo": {"refresh_hours": 168, "auto_sync": False}}}), encoding="utf-8")
    overlay.write_text(json.dumps({"sources": {"demo": {"refresh_hours": 72, "auto_sync": True}}}), encoding="utf-8")
    local.write_text(json.dumps({"sources": {"demo": {"refresh_hours": 24}}}), encoding="utf-8")
    merged = load_runtime_config(base, local, [overlay])
    assert merged["sources"]["demo"]["refresh_hours"] == 24 and merged["sources"]["demo"]["auto_sync"] is True


def test_registry_uses_packaged_ci_gold_overlay(tmp_path: Path):
    registry_file = tmp_path / "sources.csv"
    registry_file.write_text("source_id,title,domain,provider,source_url,rights_tier,access_tier,priority\ndemo,Demo,health,Demo,https://example.test,C_PUBLIC_LOCAL_INGEST,OPEN_PUBLIC,P0\n", encoding="utf-8")
    base = tmp_path / "base.json"; base.write_text(json.dumps({"defaults": {"auto_sync": False, "refresh_hours": 168}}), encoding="utf-8")
    overlay = tmp_path / "overlay.json"; overlay.write_text(json.dumps({"sources": {"demo": {"connector": "public_web", "auto_sync": True, "refresh_hours": 72}}}), encoding="utf-8")
    reg = SourceRegistry.load(registry_file, base, None, [overlay])
    spec = reg.get("demo")
    assert spec.connector == "public_web" and spec.auto_sync is True and spec.refresh_hours == 72


def test_standard_registry_auto_loads_completeness_overlay():
    reg = SourceRegistry.load(Path("registry/sources.csv"), Path("configs/runtime_sources.json"))
    known = {spec.source_id for spec in reg.all()}
    assert {"civ_famille_gender", "civ_youth", "civ_cepici", "civ_presidence", "civ_onef"}.issubset(known)


def test_qualification_requires_real_elapsed_window_clean_cycles_and_real_sync(tmp_path: Path):
    store = QualificationStore(tmp_path / "qualification.json"); store.start()
    store.data["started_at"] = (datetime.now(timezone.utc) - timedelta(days=15)).replace(microsecond=0).isoformat().replace("+00:00", "Z"); store._save()
    for _ in range(14): store.record_cycle([_Result("civ_demo", "success")])
    status = store.status()
    assert status["elapsed_days"] >= 14 and status["cycles_total"] == 14
    assert status["sync_attempts"] == 14 and status["sync_successes"] == 14
    assert status["sources_attempted"] == ["civ_demo"] and status["qualified"] is True


def test_qualification_rejects_empty_scheduler_cycles_even_after_14_days(tmp_path: Path):
    store = QualificationStore(tmp_path / "qualification.json"); store.start()
    store.data["started_at"] = (datetime.now(timezone.utc) - timedelta(days=15)).replace(microsecond=0).isoformat().replace("+00:00", "Z"); store._save()
    for _ in range(14): store.record_cycle([])
    assert store.status()["qualified"] is False and store.status()["sync_attempts"] == 0


def test_qualification_rejects_any_automatic_error(tmp_path: Path):
    store = QualificationStore(tmp_path / "qualification.json"); store.start()
    store.data["started_at"] = (datetime.now(timezone.utc) - timedelta(days=15)).replace(microsecond=0).isoformat().replace("+00:00", "Z"); store._save()
    for _ in range(13): store.record_cycle([_Result("civ_demo", "success")])
    store.record_cycle([_Result("civ_demo", "error")])
    assert store.status()["qualified"] is False and store.status()["sources_with_errors"] == ["civ_demo"]


def test_ci_gold_config_references_existing_registry_sources():
    registry = SourceRegistry.load(Path("registry/sources.csv"), Path("configs/runtime_sources.json"))
    known = {spec.source_id for spec in registry.all()}
    coverage = json.loads(Path("configs/ci_coverage.json").read_text(encoding="utf-8"))
    missing = sorted({sid for row in coverage["domains"] for sid in row.get("source_ids", []) if sid not in known})
    assert missing == []
    assert len(coverage["domains"]) >= 50


def test_needs_ocr_sidecar_is_written_without_running_ocr(tmp_path: Path):
    pdf = tmp_path / "scan.pdf"; pdf.write_bytes(b"%PDF-placeholder")
    sidecar = _write_needs_ocr({"local_path": str(pdf)}, source_id="civ_demo", source_url="https://example.test/scan.pdf", sha256="abc123", text_chars=0)
    assert sidecar is not None
    payload = json.loads(Path(sidecar).read_text(encoding="utf-8"))
    assert payload["status"] == "NEEDS_OCR" and payload["automatic_ocr"] is False


def test_data_gouv_discoveries_reports_unmapped_without_auto_ingest(monkeypatch):
    rows = [
        {"id": "known-dataset", "title": "Known", "__ivoiredata_source_url": "https://data.gouv.ci/datasets/known-dataset", "__ivoiredata_primary_domain": "health"},
        {"id": "new-dataset", "title": "New", "__ivoiredata_source_url": "https://data.gouv.ci/datasets/new-dataset", "__ivoiredata_primary_domain": "industry"},
    ]
    monkeypatch.setattr(discoveries_module, "query_source_sql", lambda *args, **kwargs: rows)
    known = SourceSpec(source_id="civ_known", title="Known", domain="health", provider="data.gouv.ci", source_url="https://data.gouv.ci/datasets/known-dataset", rights_tier="A_REDISTRIBUTABLE", access_tier="OPEN", priority="P0", connector="data_gouv_ci")
    catalog = SourceSpec(source_id="civ_datagouv_catalog", title="Catalog", domain="multidomain", provider="data.gouv.ci", source_url="https://data.gouv.ci/datasets", rights_tier="A_REDISTRIBUTABLE", access_tier="OPEN", priority="P0", connector="data_gouv_ci")
    class FakeRegistry:
        def all(self): return [catalog, known]
    class FakeEngine:
        registry = FakeRegistry(); settings = Settings()
    result = data_gouv_discoveries(FakeEngine(), limit=10)
    assert result["discovered_datasets"] == 2 and result["mapped_datasets"] == 1 and result["unmapped_datasets"] == 1
    assert result["auto_ingest_new_discoveries"] is False and result["rows"][0]["dataset_id"] == "new-dataset"
