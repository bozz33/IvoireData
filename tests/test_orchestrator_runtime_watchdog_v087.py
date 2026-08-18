from __future__ import annotations

from pathlib import Path

from ivoiredata.settings import Settings
from ivoiredata.state_io import atomic_write_json
from ivoiredata.technology_harvester import HarvestCandidate, TechnologyHarvestQueue
from ivoiredata.technology_orchestrator_runtime import IndustrialTechnologyOrchestrator


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        registry_path=tmp_path / "registry.csv",
        ci_gold_registry_path=tmp_path / "ci.csv",
        programming_docs_registry_path=tmp_path / "docs.csv",
        runtime_config_path=tmp_path / "runtime.json",
        ci_gold_runtime_path=tmp_path / "ci-runtime.json",
        programming_docs_runtime_path=tmp_path / "docs-runtime.json",
        ci_coverage_path=tmp_path / "coverage.json",
        user_agent="IvoireData-test",
    )


def test_runtime_stage_exposes_active_call_and_heartbeats_lease(tmp_path):
    settings = _settings(tmp_path)
    queue = TechnologyHarvestQueue(settings.state_dir / "technology_harvest.sqlite3")
    queue.upsert_many([HarvestCandidate("repo1.maven.org", "demo", "test")])
    try:
        orchestrator = IndustrialTechnologyOrchestrator(queue=queue, settings=settings)
        acquired, _ = orchestrator.acquire_lease("runtime-test")
        assert acquired
        first_expiry = orchestrator._meta("lease_expires_at")

        seen = {}

        def runner(registry: str, limit: int):
            seen["stage"] = orchestrator._meta("active_stage")
            seen["registry"] = orchestrator._meta("active_registry")
            seen["started_at"] = orchestrator._meta("active_call_started_at")
            return {"selected": 1, "processed": 1, "outcomes": [{"status": "OK"}]}

        result = orchestrator._run_fair_stage(
            stage="runtime-observability",
            registries=["repo1.maven.org"],
            budget=1,
            quantum=1,
            runner=runner,
        )
        assert result["processed"] == 1
        assert seen["stage"] == "runtime-observability"
        assert seen["registry"] == "repo1.maven.org"
        assert seen["started_at"]
        assert orchestrator._meta("active_stage") is None
        assert orchestrator._meta("active_registry") is None
        assert orchestrator._meta("active_call_started_at") is None
        assert orchestrator._meta("lease_expires_at") >= first_expiry
        orchestrator.release_lease("runtime-test")
    finally:
        queue.close()


def test_runtime_audit_surfaces_fetch_watchdog_state(tmp_path):
    settings = _settings(tmp_path)
    queue = TechnologyHarvestQueue(settings.state_dir / "technology_harvest.sqlite3")
    try:
        atomic_write_json(
            settings.state_dir / "technology_fetch_active.json",
            {
                "status": "RUNNING",
                "source_id": "techdocs-demo",
                "package_registry": "repo1.maven.org",
                "package_name": "demo",
                "started_at": "2026-08-18T04:00:00Z",
                "hard_timeout_seconds": 900,
                "source_lock_timeout_seconds": 120,
            },
        )
        orchestrator = IndustrialTechnologyOrchestrator(queue=queue, settings=settings)
        audit = orchestrator.audit(top=3)
        assert audit["fetch_watchdog"]["status"] == "RUNNING"
        assert audit["fetch_watchdog"]["package_name"] == "demo"
        assert "active_call" in audit
    finally:
        queue.close()
