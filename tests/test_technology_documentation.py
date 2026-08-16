from __future__ import annotations

from pathlib import Path

import pytest

from ivoiredata.technology_authority import OfficialAuthorityResolver
from ivoiredata.technology_documentation import (
    DocumentationTargetResolver,
    canonical_documentation_url,
)
from ivoiredata.technology_harvester import HarvestCandidate, TechnologyHarvestQueue
from ivoiredata.technology_qualification import TechnologyQualificationEngine


def _native(
    name: str,
    *,
    docs: str | None = "https://docs.example.test/project",
    website: str | None = "https://example.test",
    repository: str = "https://github.com/example/project",
) -> dict:
    return {
        "authority_source": "native",
        "native_registry_url": f"https://registry.example.test/{name}",
        "name": name,
        "latest_stable_version": "2.4.1",
        "canonical_repository": repository,
        "documentation_url": docs,
        "official_website": website,
        "downloads_total": 100_000_000,
        "downloads_recent": 10_000_000,
        "dependents_count": 100_000,
    }


def _verified_pipeline(
    queue: TechnologyHarvestQueue,
    *,
    registry: str = "npmjs.org",
    name: str = "project",
    docs: str | None = "https://docs.example.test/project",
    website: str | None = "https://example.test",
    repository: str = "https://github.com/example/project",
) -> None:
    queue.upsert_many([HarvestCandidate(registry, name, "seed", 95)])
    qualifier = TechnologyQualificationEngine(
        queue=queue,
        user_agent="test",
        native_resolver=lambda _registry, package: _native(
            package,
            docs=docs,
            website=website,
            repository=repository,
        ),
    )
    qualified = qualifier.run(limit=1, registry=registry)
    assert qualified["ready_for_authority"] == 1
    authority = OfficialAuthorityResolver(
        queue=queue,
        user_agent="test",
        crosscheck_resolver=lambda row, native: {
            "ecosystems": {"repository_url": repository},
            "deps_package": {},
            "deps_version": {},
            "deps_links": {},
            "sources": ["ecosyste.ms"],
            "errors": [],
        },
    )
    verified = authority.run(limit=1, registry=registry)
    assert verified["verified"] == 1


def test_canonical_documentation_url_removes_fragment_and_tracking_only():
    url = canonical_documentation_url(
        "HTTPS://Docs.Example.COM//guide/?lang=fr&utm_source=test&ref=campaign#install"
    )
    assert url == "https://docs.example.com/guide?lang=fr"


def test_verified_authority_materializes_versioned_language_target(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _verified_pipeline(
            queue,
            registry="pypi.org",
            name="Django",
            docs="https://docs.djangoproject.com/en/stable/?utm_source=registry#top",
            website="https://www.djangoproject.com",
            repository="https://github.com/django/django",
        )
        resolver = DocumentationTargetResolver(queue=queue)
        result = resolver.run(limit=10, registry="pypi")
        assert result["processed"] == 1
        assert result["ready_for_docs_connector"] == 1
        outcome = result["outcomes"][0]
        assert outcome["language"] == "Python"
        assert outcome["version"] == "2.4.1"
        assert outcome["target_url"] == "https://docs.djangoproject.com/en/stable"
        assert outcome["purl"] == "pkg:pypi/Django"
        assert outcome["source_id"].startswith("techdocs-pypi-org-django-")
    finally:
        queue.close()


def test_same_official_site_relation_gets_verified_site_confidence(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _verified_pipeline(
            queue,
            docs="https://docs.example.test/manual",
            website="https://example.test",
        )
        result = DocumentationTargetResolver(queue=queue).run(limit=1)
        assert result["outcomes"][0]["confidence"] == "VERIFIED_WEBSITE_RELATION"
    finally:
        queue.close()


def test_github_documentation_path_matching_canonical_repo_gets_repo_confidence(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _verified_pipeline(
            queue,
            docs="https://github.com/example/project/tree/main/docs",
            website=None,
            repository="https://github.com/example/project",
        )
        result = DocumentationTargetResolver(queue=queue).run(limit=1)
        assert result["outcomes"][0]["confidence"] == "VERIFIED_REPOSITORY_RELATION"
    finally:
        queue.close()


def test_missing_docs_url_falls_back_to_verified_website_discovery(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _verified_pipeline(
            queue,
            docs=None,
            website="https://example.test/project",
        )
        result = DocumentationTargetResolver(queue=queue).run(limit=1)
        assert result["discovery_required"] == 1
        assert result["outcomes"][0]["target_url"] == "https://example.test/project"
        row = queue.db.execute(
            "SELECT target_kind,target_status FROM documentation_targets WHERE name='project'"
        ).fetchone()
        assert row["target_kind"] == "OFFICIAL_WEBSITE_DISCOVERY"
        assert row["target_status"] == "DOCS_DISCOVERY_REQUIRED"
    finally:
        queue.close()


def test_missing_docs_and_website_falls_back_to_verified_repository(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _verified_pipeline(queue, docs=None, website=None)
        result = DocumentationTargetResolver(queue=queue).run(limit=1)
        assert result["discovery_required"] == 1
        assert result["outcomes"][0]["target_url"] == "https://github.com/example/project"
        row = queue.db.execute(
            "SELECT target_kind FROM documentation_targets WHERE name='project'"
        ).fetchone()
        assert row["target_kind"] == "CANONICAL_REPOSITORY_DISCOVERY"
    finally:
        queue.close()


def test_unverified_or_probable_authority_never_materializes_docs_target(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        queue.upsert_many([HarvestCandidate("npmjs.org", "project", "seed", 95)])
        qualifier = TechnologyQualificationEngine(
            queue=queue,
            user_agent="test",
            native_resolver=lambda registry, name: _native(name),
        )
        assert qualifier.run(limit=1)["ready_for_authority"] == 1
        authority = OfficialAuthorityResolver(
            queue=queue,
            user_agent="test",
            crosscheck_resolver=lambda row, native: {
                "ecosystems": {},
                "deps_package": {},
                "deps_version": {},
                "deps_links": {},
                "sources": ["ecosyste.ms"],
                "errors": [],
            },
        )
        assert authority.run(limit=1)["probable"] == 1
        result = DocumentationTargetResolver(queue=queue).run(limit=10)
        assert result["selected"] == 0
        assert queue.db.execute("SELECT COUNT(*) FROM documentation_targets").fetchone()[0] == 0
    finally:
        queue.close()


def test_unchanged_authority_costs_zero_target_work_and_new_generation_recomputes(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _verified_pipeline(queue)
        resolver = DocumentationTargetResolver(queue=queue)
        assert resolver.run(limit=1)["processed"] == 1
        assert resolver.run(limit=1)["processed"] == 0

        # Simulate a later verified authority generation even if its timestamp happens
        # to collide within the same one-second tick.
        with queue.db:
            queue.db.execute(
                "UPDATE authority_results SET attempts=attempts+1 WHERE registry='npmjs.org' AND name='project'"
            )
        assert resolver.run(limit=1)["processed"] == 1
    finally:
        queue.close()


def test_audit_groups_targets_by_language_and_total_is_not_top_window(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _verified_pipeline(queue, registry="npmjs.org", name="one")
        _verified_pipeline(queue, registry="pypi.org", name="two")
        resolver = DocumentationTargetResolver(queue=queue)
        assert resolver.run(limit=10)["processed"] == 2
        audit = resolver.audit(top=1)
        assert audit["schema_version"] == 1
        assert audit["targets"] == 2
        assert audit["ready_for_docs_connector"] == 2
        assert audit["by_language"]["JavaScript/TypeScript"]["ready"] == 1
        assert audit["by_language"]["Python"]["ready"] == 1
        assert len(audit["top_ready"]) == 1
    finally:
        queue.close()


def test_unlimited_documentation_target_resolution_is_rejected(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        resolver = DocumentationTargetResolver(queue=queue)
        with pytest.raises(ValueError, match="intentionally bounded"):
            resolver.run(limit=0)
    finally:
        queue.close()
