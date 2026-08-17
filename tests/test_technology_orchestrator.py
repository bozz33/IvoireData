from __future__ import annotations

from pathlib import Path

from ivoiredata.settings import Settings
from ivoiredata.technology_harvester import HarvestCandidate, TechnologyHarvestQueue
from ivoiredata.technology_orchestrator import IndustrialTechnologyOrchestrator


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


def _queue(tmp_path: Path) -> TechnologyHarvestQueue:
    queue = TechnologyHarvestQueue(tmp_path / "state" / "technology_harvest.sqlite3")
    queue.upsert_many(
        [
            HarvestCandidate("npmjs.org", "npm-a", "test"),
            HarvestCandidate("repo1.maven.org", "maven-a", "test"),
            HarvestCandidate("crates.io", "crate-a", "test"),
        ]
    )
    return queue


def test_stage_quantum_rotates_fairly_across_registries(tmp_path):
    queue = _queue(tmp_path)
    try:
        orchestrator = IndustrialTechnologyOrchestrator(queue=queue, settings=_settings(tmp_path))
        registries = ["npmjs.org", "repo1.maven.org", "crates.io"]
        calls = []

        def runner(registry: str, limit: int):
            calls.append((registry, limit))
            return {"selected": 1, "processed": 1, "outcomes": [{"status": "OK"}]}

        first = orchestrator._run_fair_stage(
            stage="test-fair",
            registries=registries,
            budget=2,
            quantum=1,
            runner=runner,
        )
        assert first["processed"] == 2
        assert [item[0] for item in calls] == ["npmjs.org", "repo1.maven.org"]

        calls.clear()
        second = orchestrator._run_fair_stage(
            stage="test-fair",
            registries=registries,
            budget=1,
            quantum=1,
            runner=runner,
        )
        assert second["processed"] == 1
        assert calls[0][0] == "crates.io"
    finally:
        queue.close()


def test_no_work_registry_does_not_starve_later_registry(tmp_path):
    queue = _queue(tmp_path)
    try:
        orchestrator = IndustrialTechnologyOrchestrator(queue=queue, settings=_settings(tmp_path))
        calls = []

        def runner(registry: str, limit: int):
            calls.append(registry)
            if registry == "npmjs.org":
                return {"selected": 0, "processed": 0, "outcomes": []}
            return {"selected": 1, "processed": 1, "outcomes": [{"status": "OK"}]}

        result = orchestrator._run_fair_stage(
            stage="test-empty",
            registries=["npmjs.org", "repo1.maven.org", "crates.io"],
            budget=2,
            quantum=1,
            runner=runner,
        )
        assert result["processed"] == 2
        assert calls[:3] == ["npmjs.org", "repo1.maven.org", "crates.io"]
        assert "npmjs.org" in result["exhausted_registries"]
    finally:
        queue.close()


def test_sqlite_lease_blocks_concurrent_orchestrators(tmp_path):
    path = tmp_path / "state" / "technology_harvest.sqlite3"
    first_queue = TechnologyHarvestQueue(path)
    second_queue = TechnologyHarvestQueue(path)
    try:
        first = IndustrialTechnologyOrchestrator(queue=first_queue, settings=_settings(tmp_path))
        second = IndustrialTechnologyOrchestrator(queue=second_queue, settings=_settings(tmp_path))

        acquired, _ = first.acquire_lease("run-a")
        assert acquired is True
        acquired, holder = second.acquire_lease("run-b")
        assert acquired is False
        assert holder["owner"] == "run-a"

        first.release_lease("run-a")
        acquired, _ = second.acquire_lease("run-b")
        assert acquired is True
        second.release_lease("run-b")
    finally:
        first_queue.close()
        second_queue.close()


def test_github_rate_limit_stops_fetch_stage_and_creates_shared_cooldown(tmp_path):
    queue = _queue(tmp_path)
    try:
        orchestrator = IndustrialTechnologyOrchestrator(queue=queue, settings=_settings(tmp_path))
        calls = []

        def runner(registry: str, limit: int):
            calls.append(registry)
            return {
                "selected": 1,
                "processed": 1,
                "outcomes": [
                    {
                        "status": "PARTIAL",
                        "stats": {
                            "github_rate_limited": True,
                            "github_retry_after_seconds": 120,
                            "github_rate_limit_reset": None,
                        },
                    }
                ],
            }

        result = orchestrator._run_fair_stage(
            stage="fetch-test",
            registries=["npmjs.org", "repo1.maven.org", "crates.io"],
            budget=3,
            quantum=1,
            runner=runner,
            stop_on_github_rate_limit=True,
        )
        assert result["processed"] == 1
        assert result["stopped_reason"] == "GITHUB_RATE_LIMIT"
        assert calls == ["npmjs.org"]
        cooldown = orchestrator._github_cooldown()
        assert cooldown is not None
        assert cooldown["reason"] == "GITHUB_RATE_LIMIT_SHARED_BACKOFF"
    finally:
        queue.close()


def test_registry_order_prefers_known_ecosystems_then_extras(tmp_path):
    queue = _queue(tmp_path)
    try:
        queue.upsert_many([HarvestCandidate("hex.pm", "beam-a", "test")])
        orchestrator = IndustrialTechnologyOrchestrator(queue=queue, settings=_settings(tmp_path))
        registries = orchestrator.registries()
        assert registries[:3] == ["npmjs.org", "crates.io", "repo1.maven.org"]
        assert registries[-1] == "hex.pm"
    finally:
        queue.close()
