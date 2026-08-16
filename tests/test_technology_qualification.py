from __future__ import annotations

from pathlib import Path

import pytest
import requests

from ivoiredata.technology_harvester import HarvestCandidate, TechnologyHarvestQueue
from ivoiredata.technology_qualification import TechnologyQualificationEngine


def _native(
    *,
    name: str,
    authority: str = "native",
    version: str = "1.2.3",
    repository: str | None = "https://github.com/example/project",
    docs: str | None = "https://docs.example.test/project",
    homepage: str | None = "https://example.test/project",
    downloads_total: int = 0,
    downloads_recent: int = 0,
    dependents_count: int = 0,
) -> dict:
    return {
        "authority_source": authority,
        "native_registry_url": f"https://registry.example.test/{name}",
        "name": name,
        "latest_stable_version": version,
        "canonical_repository": repository,
        "documentation_url": docs,
        "official_website": homepage,
        "downloads_total": downloads_total,
        "downloads_recent": downloads_recent,
        "dependents_count": dependents_count,
    }


def test_high_signal_candidate_is_prioritized_for_authority_resolution(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    queue.upsert_many([
        HarvestCandidate("pypi.org", "Django", "seed", 90),
    ])
    calls: list[tuple[str, str]] = []

    def resolver(registry: str, name: str):
        calls.append((registry, name))
        return _native(
            name=name,
            authority="pypi",
            downloads_total=100_000_000,
            downloads_recent=10_000_000,
            dependents_count=100_000,
        )

    try:
        engine = TechnologyQualificationEngine(
            queue=queue,
            user_agent="test",
            native_resolver=resolver,
        )
        result = engine.run(limit=1)
        assert result["selected"] == 1
        assert result["ready_for_authority"] == 1
        assert result["outcomes"][0]["status"] == "READY_FOR_AUTHORITY"
        assert result["outcomes"][0]["purl"] == "pkg:pypi/Django"
        assert calls == [("pypi.org", "Django")]
        assert queue.audit()["by_status"] == {"QUALIFIED": 1}

        row = queue.db.execute(
            "SELECT * FROM qualification_results WHERE registry='pypi.org' AND name='Django'"
        ).fetchone()
        assert row is not None
        assert row["qualification_score"] >= 40
        assert row["native_officiality_status"] == "VERIFIED_OFFICIAL"
    finally:
        queue.close()


def test_low_signal_valid_package_is_kept_on_demand_without_json_catalog_promotion(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    queue.upsert_many([
        HarvestCandidate("npmjs.org", "small-package", "npm-all-docs", 15),
    ])

    def resolver(registry: str, name: str):
        return _native(
            name=name,
            authority="npm",
            repository="https://github.com/example/small-package",
            docs=None,
            homepage=None,
        )

    try:
        engine = TechnologyQualificationEngine(
            queue=queue,
            user_agent="test",
            native_resolver=resolver,
        )
        result = engine.run(limit=1, registry="npm")
        assert result["by_status"] == {"QUALIFIED_ON_DEMAND": 1}
        assert result["outcomes"][0]["status"] == "QUALIFIED_ON_DEMAND"
        assert queue.audit()["by_status"] == {"QUALIFIED": 1}
    finally:
        queue.close()


def test_fair_lane_does_not_let_one_completed_registry_monopolize_batch(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    queue.upsert_many([
        HarvestCandidate("npmjs.org", "aaa", "npm-all-docs", 15),
        HarvestCandidate("npmjs.org", "bbb", "npm-all-docs", 15),
        HarvestCandidate("repo1.maven.org", "org.example:core", "maven-full-index", 15),
        HarvestCandidate("repo1.maven.org", "org.example:web", "maven-full-index", 15),
    ])
    calls: list[tuple[str, str]] = []

    def resolver(registry: str, name: str):
        calls.append((registry, name))
        return _native(name=name, authority=registry, repository="https://github.com/example/project")

    try:
        engine = TechnologyQualificationEngine(
            queue=queue,
            user_agent="test",
            native_resolver=resolver,
        )
        result = engine.run(limit=2)
        assert result["selected"] == 2
        assert {registry for registry, _ in calls} == {"npmjs.org", "repo1.maven.org"}
        assert result["by_registry"] == {"npmjs.org": 1, "repo1.maven.org": 1}
    finally:
        queue.close()


def test_real_upstream_requeue_allows_exactly_one_new_native_resolution(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    queue.upsert_many([
        HarvestCandidate("crates.io", "serde", "crates-index-bootstrap", 15),
    ])
    calls = 0

    def resolver(registry: str, name: str):
        nonlocal calls
        calls += 1
        return _native(name=name, authority="crates.io")

    try:
        engine = TechnologyQualificationEngine(
            queue=queue,
            user_agent="test",
            native_resolver=resolver,
        )
        assert engine.run(limit=1, registry="crates")["processed"] == 1
        assert calls == 1

        # Unchanged candidates are no longer PENDING, so qualification does no
        # network work on the second run.
        assert engine.run(limit=1, registry="crates")["processed"] == 0
        assert calls == 1

        # A genuine registry change requeues the package and permits one refresh.
        queue.upsert_many([
            HarvestCandidate("crates.io", "serde", "crates-index-changes", 60, requeue=True),
        ])
        assert engine.run(limit=1, registry="crates")["processed"] == 1
        assert calls == 2
    finally:
        queue.close()


def test_transient_failure_uses_backoff_and_is_not_immediately_retried(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    queue.upsert_many([
        HarvestCandidate("nuget.org", "Example.Core", "nuget-catalog", 15),
    ])
    calls = 0

    def resolver(registry: str, name: str):
        nonlocal calls
        calls += 1
        raise requests.Timeout("registry timeout")

    try:
        engine = TechnologyQualificationEngine(
            queue=queue,
            user_agent="test",
            native_resolver=resolver,
            retry_base_seconds=3600,
        )
        first = engine.run(limit=1, registry="nuget")
        assert first["by_status"] == {"RETRY": 1}
        assert first["outcomes"][0]["next_retry_at"]
        assert calls == 1

        second = engine.run(limit=1, registry="nuget")
        assert second["selected"] == 0
        assert calls == 1
    finally:
        queue.close()


def test_native_404_is_terminal_not_infinite_retry(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    queue.upsert_many([
        HarvestCandidate("pypi.org", "removed-project", "pypi-simple", 15),
    ])

    def resolver(registry: str, name: str):
        response = requests.Response()
        response.status_code = 404
        error = requests.HTTPError("404 Client Error")
        error.response = response
        raise error

    try:
        engine = TechnologyQualificationEngine(
            queue=queue,
            user_agent="test",
            native_resolver=resolver,
        )
        first = engine.run(limit=1, registry="pypi")
        assert first["by_status"] == {"NOT_FOUND": 1}
        assert queue.audit()["by_status"] == {"NOT_FOUND": 1}
        assert engine.run(limit=1, registry="pypi")["selected"] == 0
    finally:
        queue.close()


def test_unsupported_native_registry_is_deferred_once(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    queue.upsert_many([
        HarvestCandidate("pub.dev", "riverpod", "pubdev-package-names", 15),
    ])

    try:
        engine = TechnologyQualificationEngine(
            queue=queue,
            user_agent="test",
            native_resolver=lambda registry, name: None,
        )
        first = engine.run(limit=1, registry="pub")
        assert first["by_status"] == {"DEFERRED_UNSUPPORTED": 1}
        assert engine.run(limit=1, registry="pub")["selected"] == 0
    finally:
        queue.close()


def test_unlimited_qualification_is_rejected_explicitly(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        engine = TechnologyQualificationEngine(
            queue=queue,
            user_agent="test",
            native_resolver=lambda registry, name: _native(name=name),
        )
        with pytest.raises(ValueError, match="intentionally bounded"):
            engine.run(limit=0)
    finally:
        queue.close()


def test_qualification_audit_reports_ready_candidates(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    queue.upsert_many([
        HarvestCandidate("packagist.org", "laravel/framework", "popular", 95),
    ])

    try:
        engine = TechnologyQualificationEngine(
            queue=queue,
            user_agent="test",
            native_resolver=lambda registry, name: _native(
                name=name,
                authority="packagist",
                downloads_total=1_000_000_000,
                downloads_recent=10_000_000,
                dependents_count=100_000,
            ),
        )
        engine.run(limit=1)
        audit = engine.audit(top=5)
        assert audit["schema_version"] == 1
        assert audit["qualified_records"] == 1
        assert audit["by_status"] == {"READY_FOR_AUTHORITY": 1}
        assert audit["top_ready_for_authority"][0]["name"] == "laravel/framework"
    finally:
        queue.close()
