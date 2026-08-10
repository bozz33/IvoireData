from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ivoiredata.delivery import rebuild_catalog, write_source_manifest
from ivoiredata.metadata import classify_from_base, infer_document_type, infer_domains, source_metadata
from ivoiredata.models import SourceSpec
from ivoiredata.qualification import QualificationStore
from ivoiredata.registry import SourceRegistry
from ivoiredata.runtime_control import load_runtime_config
from ivoiredata.settings import Settings


def _spec(**kwargs) -> SourceSpec:
    data = {
        "source_id": "civ_demo",
        "title": "Demo",
        "domain": "health",
        "provider": "Demo Provider",
        "source_url": "https://example.test",
        "rights_tier": "C_PUBLIC_LOCAL_INGEST",
        "access_tier": "OPEN_PUBLIC",
        "priority": "P0",
        "connector": "public_web",
        "refresh_hours": 72,
        "auto_sync": True,
        "enabled": True,
        "options": {},
    }
    data.update(kwargs)
    return SourceSpec(**data)


class _Result:
    def __init__(self, source_id: str, status: str):
        self.source_id = source_id
        self.status = status


def test_source_metadata_is_explicitly_cote_divoire():
    meta = source_metadata(_spec())
    assert meta["country_code"] == "CIV"
    assert meta["country_name"] == "Côte d'Ivoire"
    assert meta["primary_domain"] == "health"
    assert meta["language"] == "fr"
    assert meta["rights_tier"] == "C_PUBLIC_LOCAL_INGEST"


def test_multidomain_classifier_uses_deterministic_keywords():
    primary, secondary, confidence = infer_domains(
        "multidomain",
        "Rapport sur les hôpitaux, la santé publique et la mortalité en Côte d'Ivoire",
    )
    assert primary == "health"
    assert confidence > 0.6
    assert isinstance(secondary, list)


def test_document_type_classifier_detects_budget_and_law():
    detected = infer_document_type("https://example.ci/doc", "Budget citoyen et loi de finances 2026")
    assert detected in {"LAW", "BUDGET"}
    assert infer_document_type("https://example.ci/decret-2026", "Décret portant organisation") == "DECREE"


def test_classify_from_base_preserves_country_and_rights():
    base = source_metadata(_spec(domain="multidomain", options={"language": "fr"}))
    row = classify_from_base(base, "https://example.ci/elections", "Résultats officiels de l'élection")
    assert row["country_code"] == "CIV"
    assert row["rights_tier"] == "C_PUBLIC_LOCAL_INGEST"
    assert row["primary_domain"] == "elections"
    assert row["retrieved_at"].endswith("Z")


def test_manifest_v3_and_catalog_are_country_aware(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "lake",
        state_dir=tmp_path / "state",
        ci_gold_runtime_path=tmp_path / "ci_gold.json",
        ci_coverage_path=tmp_path / "coverage.json",
    )
    spec = _spec()
    manifest = write_source_manifest(
        settings,
        spec,
        status="success",
        connector=spec.connector,
        started_at="2026-08-10T00:00:00Z",
        finished_at="2026-08-10T00:01:00Z",
    )
    assert manifest["schema_version"] == 3
    assert manifest["country_code"] == "CIV"
    assert manifest["metadata"]["primary_domain"] == "health"

    catalog = rebuild_catalog(settings, [spec])
    assert catalog["schema_version"] == 3
    assert catalog["country_code"] == "CIV"
    assert catalog["domain_index"]["health"] == ["civ_demo"]


def test_packaged_overlay_precedes_local_override(tmp_path: Path):
    base = tmp_path / "base.json"
    overlay = tmp_path / "overlay.json"
    local = tmp_path / "local.json"
    base.write_text(json.dumps({"sources": {"demo": {"refresh_hours": 168, "auto_sync": False}}}), encoding="utf-8")
    overlay.write_text(json.dumps({"sources": {"demo": {"refresh_hours": 72, "auto_sync": True}}}), encoding="utf-8")
    local.write_text(json.dumps({"sources": {"demo": {"refresh_hours": 24}}}), encoding="utf-8")
    merged = load_runtime_config(base, local, [overlay])
    assert merged["sources"]["demo"]["refresh_hours"] == 24
    assert merged["sources"]["demo"]["auto_sync"] is True


def test_registry_uses_packaged_ci_gold_overlay(tmp_path: Path):
    registry_file = tmp_path / "sources.csv"
    registry_file.write_text(
        "source_id,title,domain,provider,source_url,rights_tier,access_tier,priority\n"
        "demo,Demo,health,Demo,https://example.test,C_PUBLIC_LOCAL_INGEST,OPEN_PUBLIC,P0\n",
        encoding="utf-8",
    )
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"defaults": {"auto_sync": False, "refresh_hours": 168}}), encoding="utf-8")
    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps({"sources": {"demo": {"connector": "public_web", "auto_sync": True, "refresh_hours": 72}}}), encoding="utf-8")
    reg = SourceRegistry.load(registry_file, base, None, [overlay])
    spec = reg.get("demo")
    assert spec.connector == "public_web"
    assert spec.auto_sync is True
    assert spec.refresh_hours == 72


def test_qualification_requires_real_elapsed_window_clean_cycles_and_real_sync(tmp_path: Path):
    path = tmp_path / "qualification.json"
    store = QualificationStore(path)
    store.start()
    store.data["started_at"] = (datetime.now(timezone.utc) - timedelta(days=15)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    store._save()
    for _ in range(14):
        store.record_cycle([_Result("civ_demo", "success")])
    status = store.status()
    assert status["elapsed_days"] >= 14
    assert status["cycles_total"] == 14
    assert status["sync_attempts"] == 14
    assert status["sync_successes"] == 14
    assert status["sources_attempted"] == ["civ_demo"]
    assert status["qualified"] is True


def test_qualification_rejects_empty_scheduler_cycles_even_after_14_days(tmp_path: Path):
    store = QualificationStore(tmp_path / "qualification.json")
    store.start()
    store.data["started_at"] = (datetime.now(timezone.utc) - timedelta(days=15)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    store._save()
    for _ in range(14):
        store.record_cycle([])
    status = store.status()
    assert status["cycles_total"] == 14
    assert status["sync_attempts"] == 0
    assert status["qualified"] is False


def test_qualification_rejects_any_automatic_error(tmp_path: Path):
    store = QualificationStore(tmp_path / "qualification.json")
    store.start()
    store.data["started_at"] = (datetime.now(timezone.utc) - timedelta(days=15)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    store._save()
    for _ in range(13):
        store.record_cycle([_Result("civ_demo", "success")])
    store.record_cycle([_Result("civ_demo", "error")])
    status = store.status()
    assert status["sync_errors"] == 1
    assert status["sources_with_errors"] == ["civ_demo"]
    assert status["qualified"] is False


def test_ci_gold_config_references_existing_registry_sources():
    registry = SourceRegistry.load(
        Path("registry/sources.csv"),
        Path("configs/runtime_sources.json"),
        None,
        [Path("configs/ci_gold_sources.json")],
    )
    known = {spec.source_id for spec in registry.all()}
    coverage = json.loads(Path("configs/ci_coverage.json").read_text(encoding="utf-8"))
    missing = sorted({sid for row in coverage["domains"] for sid in row.get("source_ids", []) if sid not in known})
    assert missing == []
    assert "civ_sgg_official_texts" in known
    assert "civ_dgbf_budget" in known
    assert "civ_cei" in known
    assert "civ_ageroute" in known
    assert "civ_anare" in known
