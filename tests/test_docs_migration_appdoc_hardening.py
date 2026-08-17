from __future__ import annotations

import json
from pathlib import Path

import requests

from ivoiredata.delivery import safe_name, source_paths
from ivoiredata.models import SourceSpec, SyncResult
from ivoiredata.settings import Settings
from ivoiredata.technology_authority import OfficialAuthorityResolver as LegacyAuthorityResolver
from ivoiredata.technology_documentation import DocumentationTargetResolver
from ivoiredata.technology_documentation_discovery_runtime import ActiveDocumentationDiscovery
from ivoiredata.technology_documentation_fetch_v2 import DynamicDocumentationFetcher
from ivoiredata.technology_harvester import HarvestCandidate, TechnologyHarvestQueue
from ivoiredata.technology_qualification import TechnologyQualificationEngine as LegacyQualificationEngine


class FakeResponse:
    def __init__(self, url: str, *, status: int = 200, payload=None):
        self.url = url
        self.status_code = status
        self._payload = payload
        self.content = json.dumps(payload if payload is not None else {}).encode()
        self.headers = {
            "content-type": "application/json",
            "content-length": str(len(self.content)),
        }

    def json(self):
        return self._payload if self._payload is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} error for {self.url}")
            error.response = self
            raise error


class FakeSession:
    def __init__(self, routes: dict[str, FakeResponse]):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str, **kwargs):
        self.calls.append(url)
        return self.routes.get(url, FakeResponse(url, status=404, payload={}))


def _public_dns(host: str) -> list[str]:
    return ["93.184.216.34"]


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


def test_persisted_ready_appdoc_target_is_forced_back_through_active_discovery(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    name = "com.example:project"
    repository = "https://github.com/example/project"
    appdoc = "https://appdoc.app/artifact/com.example/project"
    try:
        queue.upsert_many([
            HarvestCandidate("repo1.maven.org", name, "legacy", 85),
        ])
        qualifier = LegacyQualificationEngine(
            queue=queue,
            user_agent="test",
            native_resolver=lambda registry, package: {
                "authority_source": "maven",
                "native_registry_url": "https://repo1.maven.org/maven2/example/maven-metadata.xml",
                "name": package,
                "latest_stable_version": "1.0.0",
                "canonical_repository": repository,
                "documentation_url": appdoc,
                "official_website": "https://project.example/",
                "downloads_total": 1_000_000,
                "dependents_count": 10_000,
            },
        )
        assert qualifier.run(limit=1, registry="maven")["ready_for_authority"] == 1
        authority = LegacyAuthorityResolver(
            queue=queue,
            user_agent="test",
            crosscheck_resolver=lambda row, native: {
                "ecosystems": {"repository_url": repository},
                "deps_package": {},
                "deps_version": {},
                "deps_links": {"SOURCE_REPO": repository},
                "sources": ["ecosyste.ms", "deps.dev"],
                "errors": [],
            },
        )
        assert authority.run(limit=1, registry="maven")["verified"] == 1
        targets = DocumentationTargetResolver(queue=queue)
        assert targets.run(limit=1, registry="maven")["ready_for_docs_connector"] == 1
        before = queue.db.execute(
            "SELECT target_url,target_status FROM documentation_targets WHERE name=?",
            (name,),
        ).fetchone()
        assert before["target_url"] == appdoc
        assert before["target_status"] == "READY_FOR_DOCS_CONNECTOR"

        routes = {
            "https://api.github.com/repos/example/project": FakeResponse(
                "https://api.github.com/repos/example/project",
                payload={
                    "html_url": repository,
                    "default_branch": "main",
                    "homepage": "",
                },
            ),
            "https://api.github.com/repos/example/project/contents": FakeResponse(
                "https://api.github.com/repos/example/project/contents",
                payload=[
                    {
                        "type": "dir",
                        "name": "docs",
                        "path": "docs",
                        "html_url": "https://github.com/example/project/tree/main/docs",
                    }
                ],
            ),
            "https://api.github.com/repos/example/project/readme": FakeResponse(
                "https://api.github.com/repos/example/project/readme",
                status=404,
                payload={},
            ),
        }
        session = FakeSession(routes)
        discovery = ActiveDocumentationDiscovery(
            queue=queue,
            user_agent="test",
            session=session,
            host_resolver=_public_dns,
        )
        result = discovery.run(limit=1, registry="maven")
        assert result["selected"] == 1
        assert result["discovered"] == 1
        assert result["outcomes"][0]["selected_url"] == "https://github.com/example/project/tree/main/docs"
        assert appdoc not in session.calls

        after = queue.db.execute(
            "SELECT target_url,target_status,target_confidence,evidence_json FROM documentation_targets WHERE name=?",
            (name,),
        ).fetchone()
        assert after["target_url"] == "https://github.com/example/project/tree/main/docs"
        assert after["target_status"] == "READY_FOR_DOCS_CONNECTOR"
        assert after["target_confidence"] == "ACTIVE_DISCOVERY_VERIFIED"
        assert "REGISTRY_LANDING_REJECTED_AS_DOCUMENTATION" in after["evidence_json"]
        assert discovery.audit(top=10)["weak_registry_targets_still_ready"] == 0
    finally:
        queue.close()


def test_changed_root_is_migration_due_even_when_resolved_timestamps_are_equal(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    settings = _settings(tmp_path)
    logical = "techdocs-repo1-maven-org-project-001"
    old_url = "https://central.sonatype.com/artifact/com.example/project/1.0.0"
    new_url = "https://github.com/example/project/tree/main/docs"
    same_tick = "2026-08-17T13:30:00Z"
    calls: list[SourceSpec] = []
    try:
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
                    "1.0.0",
                    "https://github.com/example/project",
                    "https://project.example",
                    new_url,
                    "CANONICAL_REPOSITORY_DOCS_DIRECTORY",
                    "AUTO",
                    "READY_FOR_DOCS_CONNECTOR",
                    "ACTIVE_DISCOVERY_VERIFIED",
                    "[]",
                    same_tick,
                    1,
                    same_tick,
                    same_tick,
                ),
            )

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
                    }
                ),
                encoding="utf-8",
            )
            return SyncResult(spec.source_id, "success", "start", "finish", "official_docs", "ok")

        fetcher = DynamicDocumentationFetcher(
            queue=queue,
            settings=settings,
            syncer=syncer,
            static_specs=[],
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
                    same_tick,
                    same_tick,
                    same_tick,
                    same_tick,
                ),
            )
        old_root = settings.data_dir / "programming_docs" / "jvm_java" / safe_name(logical)
        old_root.mkdir(parents=True, exist_ok=True)
        (old_root / "central-canary.txt").write_text("legacy Central", encoding="utf-8")

        before = fetcher.audit(top=10)
        assert before["due_target_migrations"] == 1
        assert before["target_migrations"] == 0

        result = fetcher.run(limit=1, registry="maven")
        assert result["selected"] == 1
        assert result["success"] == 1
        assert len(calls) == 1
        assert calls[0].source_url == new_url
        assert calls[0].source_id.startswith(logical + "-g")
        assert not old_root.exists()

        after = fetcher.audit(top=10)
        assert after["due_target_migrations"] == 0
        assert after["target_migrations"] == 1
        assert after["pending_target_migrations"] == 0
        superseded = list(
            (settings.data_dir / "programming_docs" / "_superseded" / safe_name(logical)).rglob(
                "central-canary.txt"
            )
        )
        assert len(superseded) == 1
    finally:
        queue.close()
