from __future__ import annotations

from pathlib import Path

from ivoiredata.technology_authority_v2 import OfficialAuthorityResolver
from ivoiredata.technology_harvester import HarvestCandidate, TechnologyHarvestQueue
from ivoiredata.technology_qualification import TechnologyQualificationEngine as QualificationV1


def _qualified_legacy_maven(
    queue: TechnologyHarvestQueue,
    *,
    name: str,
    repository: str,
    documentation_url: str | None = None,
    official_website: str | None = None,
) -> None:
    queue.upsert_many([
        HarvestCandidate("repo1.maven.org", name, "seed", 85),
    ])
    qualifier = QualificationV1(
        queue=queue,
        user_agent="test",
        native_resolver=lambda registry, package: {
            "authority_source": "maven",
            "native_registry_url": "https://repo1.maven.org/maven2/example/maven-metadata.xml",
            "name": package,
            "latest_stable_version": "1.2.3",
            "canonical_repository": repository,
            "documentation_url": documentation_url,
            "official_website": official_website,
        },
    )
    assert qualifier.run(limit=1, registry="maven")["ready_for_authority"] == 1


def test_legacy_central_metadata_is_sanitized_again_at_authority_boundary(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    central = "https://central.sonatype.com/artifact/com.example/core/1.2.3"
    repository = "https://github.com/example/core"
    try:
        _qualified_legacy_maven(
            queue,
            name="com.example:core",
            repository=repository,
            documentation_url=central,
            official_website=central,
        )

        authority = OfficialAuthorityResolver(
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
        result = authority.run(limit=1, registry="maven")
        assert result["verified"] == 1
        row = queue.db.execute(
            "SELECT documentation_url,official_website,canonical_repository FROM authority_results"
        ).fetchone()
        assert row["documentation_url"] is None
        assert row["official_website"] is None
        assert row["canonical_repository"] == repository
    finally:
        queue.close()


def test_appdoc_from_secondary_crosschecks_is_not_promoted_as_official_docs(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    repository = "https://github.com/example/core"
    appdoc = "https://appdoc.app/artifact/com.example/core"
    try:
        _qualified_legacy_maven(
            queue,
            name="com.example:core",
            repository=repository,
            documentation_url=None,
            official_website="https://project.example/",
        )
        authority = OfficialAuthorityResolver(
            queue=queue,
            user_agent="test",
            crosscheck_resolver=lambda row, native: {
                "ecosystems": {
                    "repository_url": repository,
                    "documentation_url": appdoc,
                },
                "deps_package": {},
                "deps_version": {},
                "deps_links": {
                    "SOURCE_REPO": repository,
                    "DOCUMENTATION": appdoc,
                },
                "sources": ["ecosyste.ms", "deps.dev"],
                "errors": [],
            },
        )
        result = authority.run(limit=1, registry="maven")
        assert result["verified"] == 1
        row = queue.db.execute(
            "SELECT documentation_url,official_website,officiality_score,evidence_json FROM authority_results"
        ).fetchone()
        assert row["documentation_url"] is None
        assert row["official_website"] == "https://project.example/"
        assert "DOCUMENTATION_URL" not in str(row["evidence_json"])
    finally:
        queue.close()
