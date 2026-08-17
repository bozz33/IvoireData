from __future__ import annotations

from pathlib import Path

from ivoiredata.delivery import safe_name
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


def _new_target(queue: TechnologyHarvestQueue, logical: str, new_url: str) -> None:
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
                "repo1.maven.org",
                "com.example:project",
                logical,
                "jvm",
                "JVM/Java",
                "com.example:project",
                "pkg:maven/com.example/project",
                "1.1.0",
                "https://github.com/example/project",
                "https://project.example",
                new_url,
                "OFFICIAL_WEBSITE_DOCS_LINK",
                "AUTO",
                "READY_FOR_DOCS_CONNECTOR",
                "ACTIVE_DISCOVERY_VERIFIED",
                "[]",
                "2026-08-17T10:00:00Z",
                1,
                "2026-08-17T10:00:00Z",
                "2026-08-17T13:00:00Z",
            ),
        )


def test_failed_replacement_keeps_old_corpus_live_and_completes_quarantine_after_retry(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    settings = _settings(tmp_path)
    logical = "techdocs-repo1-maven-org-project-001"
    old_url = "https://central.sonatype.com/artifact/com.example/project/1.0.0"
    new_url = "https://docs.project.example/reference"
    attempts = 0

    def syncer(spec: SourceSpec, force: bool) -> SyncResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return SyncResult(
                spec.source_id,
                "error",
                "start",
                "finish",
                "official_docs",
                "temporary failure",
            )
        # Injected success path can be considered complete by overriding _stats below.
        return SyncResult(
            spec.source_id,
            "success",
            "start",
            "finish",
            "official_docs",
            "ok",
        )

    try:
        _new_target(queue, logical, new_url)
        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=settings,
            syncer=syncer,
            static_specs=[],
            retry_base_seconds=1,
        )
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
        (old_root / "old.txt").write_text("old corpus", encoding="utf-8")

        first = fetcher.run(limit=1, registry="maven")
        assert first["retry"] == 1
        assert old_root.exists()
        pending = queue.db.execute(
            "SELECT migration_status,quarantine_path FROM documentation_target_migrations"
        ).fetchone()
        assert pending["migration_status"] == "PENDING"
        assert pending["quarantine_path"] is None

        # Make the persisted retry immediately due and provide complete official-docs
        # stats for the successful retry.
        with queue.db:
            queue.db.execute(
                "UPDATE documentation_fetch_state SET next_retry_at='2000-01-01T00:00:00Z'"
            )
        fetcher._stats = lambda spec: {
            "discovery_complete": True,
            "discovery_truncated": False,
            "failed": 0,
            "backlog_count": 0,
        }
        second = fetcher.run(limit=1, registry="maven")
        assert second["success"] == 1
        assert not old_root.exists()
        completed = queue.db.execute(
            "SELECT migration_status,quarantine_path FROM documentation_target_migrations"
        ).fetchone()
        assert completed["migration_status"] == "COMPLETED"
        assert completed["quarantine_path"]
        assert Path(completed["quarantine_path"]).joinpath("old.txt").exists()
    finally:
        queue.close()
