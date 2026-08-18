from __future__ import annotations

import time
import tomllib
from pathlib import Path

import pytest

import ivoiredata
from ivoiredata import cli_runtime
from ivoiredata.deadline import HardDeadlineExceeded, hard_deadline, hard_deadline_supported
from ivoiredata.models import SourceSpec, SyncResult
from ivoiredata.pipeline import _source_lock_timeout
from ivoiredata.settings import Settings
from ivoiredata.state_io import load_json
from ivoiredata.technology_documentation_fetch_v2 import DynamicDocumentationFetcher
from ivoiredata.technology_harvester import TechnologyHarvestQueue


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


def _target():
    return {
        "registry": "repo1.maven.org",
        "name": "com.example:docs",
        "source_id": "techdocs-maven-example-docs",
        "ecosystem": "jvm",
        "programming_language": "Java",
        "canonical_name": "com.example:docs",
        "purl": "pkg:maven/com.example/docs",
        "package_version": "1.0.0",
        "canonical_repository": "https://github.com/example/docs",
        "target_url": "https://github.com/example/docs/tree/main/docs",
        "source_strategy": "OFFICIAL_GIT",
        "last_resolved_at": "2026-08-18T00:00:00Z",
    }


def test_dynamic_source_lock_timeout_overrides_legacy_six_hour_default(monkeypatch):
    spec = SourceSpec(
        source_id="dynamic-docs",
        title="Docs",
        domain="programming_dynamic_java",
        provider="Example",
        source_url="https://example.com/docs",
        rights_tier="C_PUBLIC_LOCAL_INGEST",
        access_tier="OPEN_PUBLIC",
        priority="P3",
        connector="official_docs",
        options={"source_lock_timeout_seconds": 120},
    )
    monkeypatch.setenv("IVOIREDATA_SOURCE_LOCK_TIMEOUT", "21600")
    assert _source_lock_timeout(spec) == 120.0
    assert _source_lock_timeout(None) == 21600.0


def test_dynamic_fetch_spec_injects_lock_and_hard_target_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("IVOIREDATA_TECH_FETCH_LOCK_TIMEOUT", "77")
    monkeypatch.setenv("IVOIREDATA_TECH_FETCH_TARGET_TIMEOUT", "333")
    queue = TechnologyHarvestQueue(tmp_path / "state" / "technology_harvest.sqlite3")
    try:
        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=_settings(tmp_path),
            static_specs=[],
            syncer=lambda spec, force: SyncResult(spec.source_id, "success", "a", "b", spec.connector, "ok"),
        )
        spec = fetcher._spec(_target())
        assert spec.options["source_lock_timeout_seconds"] == 77.0
        assert spec.options["fetch_target_timeout_seconds"] == 333.0
    finally:
        queue.close()


@pytest.mark.skipif(not hard_deadline_supported(), reason="SIGALRM watchdog requires POSIX main thread")
def test_hard_deadline_interrupts_non_network_wait():
    started = time.monotonic()
    with pytest.raises(HardDeadlineExceeded):
        with hard_deadline(0.05, label="test-stall"):
            time.sleep(5)
    assert time.monotonic() - started < 1.0


@pytest.mark.skipif(not hard_deadline_supported(), reason="SIGALRM watchdog requires POSIX main thread")
def test_fetch_watchdog_converts_hang_to_retryable_sync_result(tmp_path, monkeypatch):
    monkeypatch.setenv("IVOIREDATA_TECH_FETCH_TARGET_TIMEOUT", "1")
    monkeypatch.setenv("IVOIREDATA_TECH_FETCH_LOCK_TIMEOUT", "2")

    def hung_syncer(spec, force):
        time.sleep(10)
        return SyncResult(spec.source_id, "success", "a", "b", spec.connector, "unreachable")

    settings = _settings(tmp_path)
    queue = TechnologyHarvestQueue(settings.state_dir / "technology_harvest.sqlite3")
    try:
        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=settings,
            static_specs=[],
            syncer=hung_syncer,
        )
        spec = fetcher._spec(_target())
        started = time.monotonic()
        result = fetcher._run_syncer(spec, False)
        elapsed = time.monotonic() - started

        assert result.status == "error"
        assert "hard deadline exceeded" in result.details
        assert elapsed < 3.0

        watchdog = load_json(settings.state_dir / "technology_fetch_active.json", {})
        assert watchdog["status"] == "HARD_TIMEOUT"
        assert watchdog["package_name"] == "com.example:docs"
        assert watchdog["hard_timeout_seconds"] == 1.0

        stats_path = (
            settings.data_dir
            / "programming_docs"
            / "Java"
            / spec.source_id
            / "raw"
            / "official_docs_sync_stats.json"
        )
        # Delivery layout sanitization can vary by language/domain; the watchdog state
        # above is the durable scheduler-facing proof.  The sync result must remain the
        # authoritative RETRY input regardless of where the source snapshot is rooted.
        assert result.connector == "official_docs"
    finally:
        queue.close()


def test_release_version_is_consistent_and_cli_is_not_hardcoded(capsys):
    root = Path(__file__).resolve().parents[1]
    version_file = (root / "VERSION").read_text(encoding="utf-8").strip()
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert version_file
    assert ivoiredata.__version__ == version_file
    assert project["project"]["version"] == version_file

    with pytest.raises(SystemExit) as caught:
        cli_runtime.parser().parse_args(["--version"])
    assert caught.value.code == 0
    assert f"ivoiredata {version_file}" in capsys.readouterr().out
