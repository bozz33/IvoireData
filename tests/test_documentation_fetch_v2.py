from __future__ import annotations

import json
from pathlib import Path

from ivoiredata.delivery import safe_name, source_paths
from ivoiredata.models import SourceSpec, SyncResult
from ivoiredata.settings import Settings
from ivoiredata.technology_documentation import DocumentationTargetResolver
from ivoiredata.technology_documentation_fetch_v2 import DynamicDocumentationFetcher
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
    url: str,
    resolved_at: str,
    version: str = "1.0.0",
    source_id: str = "techdocs-repo1-maven-org-project-001",
) -> None:
    DocumentationTargetResolver(queue=queue)
    existing = queue.db.execute(
        "SELECT 1 FROM documentation_targets WHERE registry='repo1.maven.org' AND name='com.example:project'"
    ).fetchone()
    if existing is None:
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
                    "repo1.maven.org",
                    "com.example:project",
                    source_id,
                    "jvm",
                    "JVM/Java",
                    "com.example:project",
                    "pkg:maven/com.example/project",
                    version,
                    "https://github.com/example/project",
                    "https://project.example",
                    url,
                    "DOCUMENTATION_URL",
                    "AUTO",
                    "READY_FOR_DOCS_CONNECTOR",
                    "ACTIVE_DISCOVERY_VERIFIED",
                    "[]",
                    "2026-08-17T10:00:00Z",
                    1,
                    "2026-08-17T10:00:00Z",
                    resolved_at,
                ),
            )
    else:
        with queue.db:
            queue.db.execute(
                """
                UPDATE documentation_targets
                SET target_url=?,package_version=?,last_resolved_at=?,
                    target_status='READY_FOR_DOCS_CONNECTOR',
                    target_confidence='ACTIVE_DISCOVERY_VERIFIED'
                WHERE registry='repo1.maven.org' AND name='com.example:project'
                """,
                (url, version, resolved_at),
            )


def _syncer(settings: Settings, calls: list[SourceSpec]):
    def sync(spec: SourceSpec, force: bool) -> SyncResult:
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
                    "downloaded": 3,
                }
            ),
            encoding="utf-8",
        )
        (raw / "new-doc-marker.txt").write_text(spec.source_url, encoding="utf-8")
        return SyncResult(
            spec.source_id,
            "success",
            "start",
            "finish",
            "official_docs",
            "ok",
        )

    return sync


def test_central_to_official_docs_change_uses_new_physical_source_and_quarantines_old_canary(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    settings = _settings(tmp_path)
    calls: list[SourceSpec] = []
    logical = "techdocs-repo1-maven-org-project-001"
    old_url = "https://central.sonatype.com/artifact/com.example/project/1.0.0"
    new_url = "https://docs.project.example/reference"
    try:
        _target(queue, url=new_url, resolved_at="2026-08-17T12:00:00Z", source_id=logical)
        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=settings,
            syncer=_syncer(settings, calls),
            static_specs=[],
        )
        # Simulate a PR #35 canary fetch: its physical id was the old logical source id.
        with queue.db:
            queue.db.execute(
                """
                INSERT INTO documentation_fetch_state(
                    registry,name,source_id,target_url,target_resolved_at,fetch_status,attempts,
                    first_attempt_at,last_attempt_at,last_success_at,stats_json,physical_source_id
                ) VALUES(?,?,?,?,?,'SUCCESS',1,?,?,?,'{}',NULL)
                """,
                (
                    "repo1.maven.org",
                    "com.example:project",
                    logical,
                    old_url,
                    "2026-08-17T10:30:00Z",
                    "2026-08-17T10:30:00Z",
                    "2026-08-17T10:31:00Z",
                    "2026-08-17T10:31:00Z",
                ),
            )
        old_root = settings.data_dir / "programming_docs" / "jvm_java" / safe_name(logical)
        old_root.mkdir(parents=True, exist_ok=True)
        (old_root / "central-canary.txt").write_text("thin Central page", encoding="utf-8")

        result = fetcher.run(limit=1, registry="maven")
        assert result["success"] == 1
        assert len(calls) == 1
        physical = calls[0].source_id
        assert physical.startswith(logical + "-g")
        assert physical != logical
        assert calls[0].source_url == new_url
        assert not old_root.exists()

        superseded = list(
            (settings.data_dir / "programming_docs" / "_superseded" / safe_name(logical)).rglob(
                "central-canary.txt"
            )
        )
        assert len(superseded) == 1
        assert superseded[0].read_text(encoding="utf-8") == "thin Central page"

        state = queue.db.execute(
            "SELECT target_url,physical_source_id,fetch_status FROM documentation_fetch_state"
        ).fetchone()
        assert state["target_url"] == new_url
        assert state["physical_source_id"] == physical
        assert state["fetch_status"] == "SUCCESS"
        migration = queue.db.execute(
            "SELECT old_target_url,new_target_url,old_physical_source_id,new_physical_source_id,quarantine_path FROM documentation_target_migrations"
        ).fetchone()
        assert migration["old_target_url"] == old_url
        assert migration["new_target_url"] == new_url
        assert migration["old_physical_source_id"] == logical
        assert migration["new_physical_source_id"] == physical
        assert migration["quarantine_path"]
    finally:
        queue.close()


def test_same_documentation_root_version_change_reuses_physical_generation_incrementally(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    settings = _settings(tmp_path)
    calls: list[SourceSpec] = []
    url = "https://docs.project.example/reference"
    try:
        _target(queue, url=url, resolved_at="2026-08-17T12:00:00Z", version="1.0.0")
        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=settings,
            syncer=_syncer(settings, calls),
            static_specs=[],
        )
        first = fetcher.run(limit=1)
        assert first["success"] == 1
        first_physical = calls[0].source_id
        assert fetcher.run(limit=1)["selected"] == 0

        _target(queue, url=url, resolved_at="2026-08-17T13:00:00Z", version="1.1.0")
        second = fetcher.run(limit=1)
        assert second["success"] == 1
        assert len(calls) == 2
        assert calls[1].source_id == first_physical
        assert calls[1].options["doc_version"] == "1.1.0"
        assert queue.db.execute("SELECT COUNT(*) FROM documentation_target_migrations").fetchone()[0] == 0
    finally:
        queue.close()


def test_fetch_audit_exposes_target_generation_migrations(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    settings = _settings(tmp_path)
    try:
        _target(queue, url="https://docs.project.example", resolved_at="2026-08-17T12:00:00Z")
        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=settings,
            syncer=_syncer(settings, []),
            static_specs=[],
        )
        assert fetcher.run(limit=1)["success"] == 1
        audit = fetcher.audit(top=10)
        assert audit["engine"] == "dynamic-documentation-fetcher-v2"
        assert audit["target_migrations"] == 0
        assert audit["covered_targets"] == 1
    finally:
        queue.close()
