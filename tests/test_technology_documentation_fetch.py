from __future__ import annotations

import json
from pathlib import Path

import pytest

from ivoiredata.delivery import source_paths
from ivoiredata.models import SourceSpec, SyncResult
from ivoiredata.settings import Settings
from ivoiredata.technology_documentation import DocumentationTargetResolver
from ivoiredata.technology_documentation_fetch import DynamicDocumentationFetcher
from ivoiredata.technology_harvester import TechnologyHarvestQueue


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data_lake",
        state_dir=tmp_path / "state",
        registry_path=tmp_path / "missing-sources.csv",
        ci_gold_registry_path=tmp_path / "missing-ci.csv",
        programming_docs_registry_path=tmp_path / "missing-programming.csv",
        runtime_config_path=tmp_path / "missing-runtime.json",
        ci_gold_runtime_path=tmp_path / "missing-ci-runtime.json",
        programming_docs_runtime_path=tmp_path / "missing-programming-runtime.json",
        ci_coverage_path=tmp_path / "missing-coverage.json",
    )


def _target(
    queue: TechnologyHarvestQueue,
    *,
    registry: str = "pypi.org",
    name: str = "project",
    source_id: str = "techdocs-pypi-project-001",
    url: str = "https://docs.example.test/project",
    language: str = "Python",
    status: str = "READY_FOR_DOCS_CONNECTOR",
) -> None:
    DocumentationTargetResolver(queue=queue)
    with queue.db:
        queue.db.execute(
            """
            INSERT INTO documentation_targets(
                registry,name,source_id,ecosystem,programming_language,canonical_name,purl,
                package_version,canonical_repository,official_website,target_url,target_kind,
                source_strategy,target_status,target_confidence,evidence_json,authority_checked_at,
                authority_attempts,first_resolved_at,last_resolved_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                registry,
                name,
                source_id,
                "python",
                language,
                name,
                f"pkg:pypi/{name}",
                "1.2.3",
                "https://github.com/example/project",
                "https://example.test",
                url,
                "DOCUMENTATION_URL",
                "AUTO",
                status,
                "AUTHORITY_DERIVED",
                "[]",
                "2026-08-16T23:00:00Z",
                1,
                "2026-08-16T23:00:00Z",
                "2026-08-16T23:00:00Z",
            ),
        )


def _successful_syncer(settings: Settings, calls: list[SourceSpec]):
    def syncer(spec: SourceSpec, force: bool) -> SyncResult:
        calls.append(spec)
        raw = source_paths(settings, spec)["raw"]
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "official_docs_sync_stats.json").write_text(
            json.dumps(
                {
                    "discovery_complete": True,
                    "discovery_truncated": False,
                    "failed": 0,
                    "backlog_count": 0,
                    "downloaded": 12,
                    "unchanged": 88,
                }
            ),
            encoding="utf-8",
        )
        return SyncResult(spec.source_id, "success", "start", "finish", "official_docs", "ok")

    return syncer


def test_ready_target_uses_standard_official_docs_spec_without_training_enablement(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    settings = _settings(tmp_path)
    calls: list[SourceSpec] = []
    try:
        _target(queue)
        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=settings,
            syncer=_successful_syncer(settings, calls),
            static_specs=[],
        )
        result = fetcher.run(limit=1)
        assert result["success"] == 1
        assert len(calls) == 1
        spec = calls[0]
        assert spec.connector == "official_docs"
        assert spec.options["programming_language"] == "Python"
        assert spec.options["doc_version"] == "1.2.3"
        assert spec.options["package_purl"] == "pkg:pypi/project"
        assert spec.options["training_eligible"] is False
        assert spec.options["license_review_status"] == "UNREVIEWED"
        assert spec.auto_sync is False
    finally:
        queue.close()


def test_static_official_docs_alias_prevents_duplicate_download(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    settings = _settings(tmp_path)
    calls: list[SourceSpec] = []
    try:
        _target(queue, url="https://docs.example.test/project/")
        static = SourceSpec(
            source_id="prog_existing",
            title="Existing docs",
            domain="programming_python",
            provider="Example",
            source_url="https://docs.example.test/project",
            rights_tier="C_PUBLIC_LOCAL_INGEST",
            access_tier="OPEN_PUBLIC",
            priority="P1",
            connector="official_docs",
            options={"programming_language": "Python"},
        )
        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=settings,
            syncer=_successful_syncer(settings, calls),
            static_specs=[static],
        )
        result = fetcher.run(limit=1)
        assert result["aliased_static"] == 1
        assert calls == []
        assert result["outcomes"][0]["alias_source_id"] == "prog_existing"
    finally:
        queue.close()


def test_identical_dynamic_target_url_is_downloaded_once_then_aliased(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    settings = _settings(tmp_path)
    calls: list[SourceSpec] = []
    try:
        _target(queue, name="one", source_id="techdocs-one", url="https://docs.example.test/shared")
        _target(queue, name="two", source_id="techdocs-two", url="https://docs.example.test/shared")
        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=settings,
            syncer=_successful_syncer(settings, calls),
            static_specs=[],
        )
        result = fetcher.run(limit=2)
        assert result["success"] == 1
        assert result["aliased_dynamic"] == 1
        assert len(calls) == 1
        alias = next(item for item in result["outcomes"] if item["status"] == "ALIASED_DYNAMIC_SOURCE")
        assert alias["alias_source_id"] == "techdocs-one"
    finally:
        queue.close()


def test_successful_connector_with_backlog_is_partial_and_not_immediately_retried(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    settings = _settings(tmp_path)
    calls = 0
    try:
        _target(queue)

        def syncer(spec: SourceSpec, force: bool) -> SyncResult:
            nonlocal calls
            calls += 1
            raw = source_paths(settings, spec)["raw"]
            raw.mkdir(parents=True, exist_ok=True)
            (raw / "official_docs_sync_stats.json").write_text(
                json.dumps(
                    {
                        "discovery_complete": False,
                        "discovery_truncated": True,
                        "failed": 0,
                        "backlog_count": 250,
                    }
                ),
                encoding="utf-8",
            )
            return SyncResult(spec.source_id, "success", "start", "finish", "official_docs", "bounded")

        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=settings,
            syncer=syncer,
            static_specs=[],
            retry_base_seconds=3600,
        )
        first = fetcher.run(limit=1)
        assert first["partial"] == 1
        assert first["outcomes"][0]["next_retry_at"]
        assert calls == 1
        second = fetcher.run(limit=1)
        assert second["selected"] == 0
        assert calls == 1
    finally:
        queue.close()


def test_error_result_uses_retry_backoff(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    settings = _settings(tmp_path)
    calls = 0
    try:
        _target(queue)

        def syncer(spec: SourceSpec, force: bool) -> SyncResult:
            nonlocal calls
            calls += 1
            return SyncResult(spec.source_id, "error", "start", "finish", "official_docs", "network failure")

        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=settings,
            syncer=syncer,
            static_specs=[],
            retry_base_seconds=3600,
        )
        first = fetcher.run(limit=1)
        assert first["retry"] == 1
        assert first["outcomes"][0]["next_retry_at"]
        assert fetcher.run(limit=1)["selected"] == 0
        assert calls == 1
    finally:
        queue.close()


def test_discovery_required_target_is_not_crawled_as_if_it_were_docs(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    settings = _settings(tmp_path)
    calls: list[SourceSpec] = []
    try:
        _target(queue, status="DOCS_DISCOVERY_REQUIRED")
        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=settings,
            syncer=_successful_syncer(settings, calls),
            static_specs=[],
        )
        assert fetcher.run(limit=10)["selected"] == 0
        assert calls == []
    finally:
        queue.close()


def test_unchanged_successful_target_costs_zero_second_fetch(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    settings = _settings(tmp_path)
    calls: list[SourceSpec] = []
    try:
        _target(queue)
        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=settings,
            syncer=_successful_syncer(settings, calls),
            static_specs=[],
        )
        assert fetcher.run(limit=1)["success"] == 1
        assert fetcher.run(limit=1)["selected"] == 0
        assert len(calls) == 1
    finally:
        queue.close()


def test_new_target_generation_reuses_same_source_id_and_runs_incremental_connector(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    settings = _settings(tmp_path)
    calls: list[SourceSpec] = []
    try:
        _target(queue)
        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=settings,
            syncer=_successful_syncer(settings, calls),
            static_specs=[],
        )
        assert fetcher.run(limit=1)["success"] == 1
        with queue.db:
            queue.db.execute(
                "UPDATE documentation_targets SET last_resolved_at='2026-08-17T00:00:00Z',package_version='1.2.4' WHERE name='project'"
            )
        assert fetcher.run(limit=1)["success"] == 1
        assert len(calls) == 2
        assert calls[0].source_id == calls[1].source_id
        assert calls[1].options["doc_version"] == "1.2.4"
    finally:
        queue.close()


def test_fetch_audit_reports_real_coverage_not_top_window(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    settings = _settings(tmp_path)
    calls: list[SourceSpec] = []
    try:
        _target(queue, name="one", source_id="techdocs-one", url="https://docs.example.test/one")
        _target(queue, name="two", source_id="techdocs-two", url="https://docs.example.test/two")
        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=settings,
            syncer=_successful_syncer(settings, calls),
            static_specs=[],
        )
        assert fetcher.run(limit=2)["success"] == 2
        audit = fetcher.audit(top=1)
        assert audit["schema_version"] == 1
        assert audit["ready_targets"] == 2
        assert audit["covered_targets"] == 2
        assert audit["remaining_targets"] == 0
        assert audit["coverage_percent"] == 100.0
        assert len(audit["recent"]) == 1
    finally:
        queue.close()


def test_unlimited_dynamic_fetch_is_rejected(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    settings = _settings(tmp_path)
    try:
        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=settings,
            syncer=lambda spec, force: SyncResult(spec.source_id, "success", "", "", "official_docs", ""),
            static_specs=[],
        )
        with pytest.raises(ValueError, match="intentionally bounded"):
            fetcher.run(limit=0)
    finally:
        queue.close()
