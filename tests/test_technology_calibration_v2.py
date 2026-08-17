from __future__ import annotations

from pathlib import Path

from ivoiredata.technology_authority_v2 import OfficialAuthorityResolver
from ivoiredata.technology_harvester import HarvestCandidate, TechnologyHarvestQueue
from ivoiredata.technology_qualification import TechnologyQualificationEngine as QualificationV1
from ivoiredata.technology_qualification_v2 import TechnologyQualificationEngine


def _maven_native(name: str = "com.example:core") -> dict:
    return {
        "authority_source": "maven",
        "native_registry_url": "https://repo1.maven.org/maven2/com/example/core/maven-metadata.xml",
        "registry_landing_url": "https://central.sonatype.com/artifact/com.example/core/1.2.3",
        "name": name,
        "latest_stable_version": "1.2.3",
        "canonical_repository": "https://github.com/example/core",
        "documentation_url": None,
        "official_website": "https://example.org/core",
    }


def test_maven_metadata_can_be_ready_for_authority_without_fake_popularity(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        queue.upsert_many([
            HarvestCandidate("repo1.maven.org", "com.example:core", "maven-full-index", 25),
        ])
        engine = TechnologyQualificationEngine(
            queue=queue,
            user_agent="test",
            native_resolver=lambda registry, name: _maven_native(name),
        )
        result = engine.run(limit=1, registry="maven")
        assert result["ready_for_authority"] == 1
        outcome = result["outcomes"][0]
        # Importance/ranking remains low because Maven did not provide popularity
        # telemetry; authority readiness is now a separate decision.
        assert int(outcome["score"]) < 40
        row = queue.db.execute(
            "SELECT importance_score,candidate_priority,native_officiality_score,evidence_json FROM qualification_results"
        ).fetchone()
        assert int(row["candidate_priority"]) == 25
        assert int(row["importance_score"]) < 40
        assert int(row["native_officiality_score"]) >= 60
        assert "MAVEN_METADATA_READY_FOR_AUTHORITY" in row["evidence_json"]
    finally:
        queue.close()


def test_recalibration_upgrades_old_maven_rows_without_native_network_call(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    legacy = {
        **_maven_native(),
        # v1 used Central as both documentation and website evidence.
        "documentation_url": "https://central.sonatype.com/artifact/com.example/core/1.2.3",
        "official_website": "https://central.sonatype.com/artifact/com.example/core/1.2.3",
    }
    try:
        queue.upsert_many([
            HarvestCandidate("repo1.maven.org", "com.example:core", "maven-full-index", 25),
        ])
        v1 = QualificationV1(
            queue=queue,
            user_agent="test",
            native_resolver=lambda registry, name: legacy,
        )
        first = v1.run(limit=1, registry="maven")
        assert first["on_demand"] == 1

        def forbidden(*args, **kwargs):
            raise AssertionError("recalibration must not call a native registry")

        v2 = TechnologyQualificationEngine(
            queue=queue,
            user_agent="test",
            native_resolver=forbidden,
        )
        recalibrated = v2.recalibrate(limit=10, registry="maven")
        assert recalibrated["network_requests"] == 0
        assert recalibrated["changed_status"] == 1
        assert recalibrated["by_after"] == {"READY_FOR_AUTHORITY": 1}
        row = queue.db.execute(
            "SELECT qualification_status,documentation_url,official_website,native_officiality_score FROM qualification_results"
        ).fetchone()
        assert row["qualification_status"] == "READY_FOR_AUTHORITY"
        # Policy fields are corrected even though raw metadata_json is preserved for provenance.
        assert row["documentation_url"] is None
        assert row["official_website"] is None
        assert int(row["native_officiality_score"]) == 60
    finally:
        queue.close()


def _qualified_row(queue: TechnologyHarvestQueue, repository: str = "https://github.com/lysine-dev/okhttp") -> None:
    queue.upsert_many([
        HarvestCandidate("repo1.maven.org", "com.squareup.okhttp3:okhttp", "seed", 85),
    ])
    engine = TechnologyQualificationEngine(
        queue=queue,
        user_agent="test",
        native_resolver=lambda registry, name: {
            "authority_source": "maven",
            "native_registry_url": "https://repo1.maven.org/maven2/x/maven-metadata.xml",
            "name": name,
            "latest_stable_version": "5.1.0",
            "canonical_repository": repository,
            "official_website": "https://lysine.dev/okhttp/",
        },
    )
    assert engine.run(limit=1, registry="maven")["ready_for_authority"] == 1


def test_github_transfer_same_repository_id_converts_textual_conflict_to_verified(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _qualified_row(queue)

        def identity(url: str):
            assert url in {
                "https://github.com/lysine-dev/okhttp",
                "https://github.com/square/okhttp",
            }
            return {
                "id": "5152285",
                "full_name": "lysine-dev/okhttp",
                "html_url": "https://github.com/lysine-dev/okhttp",
            }

        resolver = OfficialAuthorityResolver(
            queue=queue,
            user_agent="test",
            repository_identity_resolver=identity,
            crosscheck_resolver=lambda row, native: {
                "ecosystems": {"repository_url": "https://github.com/square/okhttp"},
                "deps_package": {},
                "deps_version": {},
                "deps_links": {"SOURCE_REPO": "https://github.com/square/okhttp"},
                "sources": ["ecosyste.ms", "deps.dev"],
                "errors": [],
            },
        )
        result = resolver.run(limit=1, registry="maven")
        assert result["verified"] == 1
        outcome = result["outcomes"][0]
        assert outcome["repository_conflict"] is False
        assert outcome["repository_match"] is True
        assert outcome["repository"] == "https://github.com/lysine-dev/okhttp"
        row = queue.db.execute("SELECT evidence_json FROM authority_results").fetchone()
        assert "GITHUB_REPOSITORY_TRANSFER_MATCH" in row["evidence_json"]
    finally:
        queue.close()


def test_different_github_repository_ids_remain_blocked_as_conflict(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _qualified_row(queue, repository="https://github.com/example/official")

        def identity(url: str):
            if url.endswith("example/official"):
                return {"id": "1", "full_name": "example/official", "html_url": url}
            return {"id": "2", "full_name": "attacker/fork", "html_url": url}

        resolver = OfficialAuthorityResolver(
            queue=queue,
            user_agent="test",
            repository_identity_resolver=identity,
            crosscheck_resolver=lambda row, native: {
                "ecosystems": {"repository_url": "https://github.com/attacker/fork"},
                "deps_package": {},
                "deps_version": {},
                "deps_links": {"SOURCE_REPO": "https://github.com/attacker/fork"},
                "sources": ["ecosyste.ms", "deps.dev"],
                "errors": [],
            },
        )
        result = resolver.run(limit=1, registry="maven")
        assert result["conflicts"] == 1
        assert result["outcomes"][0]["repository_conflict"] is True
    finally:
        queue.close()


def test_conflict_recheck_reuses_qualification_metadata_and_can_resolve_transfer(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _qualified_row(queue)
        first = OfficialAuthorityResolver(
            queue=queue,
            user_agent="test",
            repository_identity_resolver=lambda url: {"id": url, "full_name": url, "html_url": url},
            crosscheck_resolver=lambda row, native: {
                "ecosystems": {"repository_url": "https://github.com/square/okhttp"},
                "deps_package": {},
                "deps_version": {},
                "deps_links": {"SOURCE_REPO": "https://github.com/square/okhttp"},
                "sources": ["ecosyste.ms", "deps.dev"],
                "errors": [],
            },
        )
        assert first.run(limit=1)["conflicts"] == 1

        resolver = OfficialAuthorityResolver(
            queue=queue,
            user_agent="test",
            repository_identity_resolver=lambda url: {
                "id": "5152285",
                "full_name": "lysine-dev/okhttp",
                "html_url": "https://github.com/lysine-dev/okhttp",
            },
            crosscheck_resolver=lambda row, native: {
                "ecosystems": {"repository_url": "https://github.com/square/okhttp"},
                "deps_package": {},
                "deps_version": {},
                "deps_links": {"SOURCE_REPO": "https://github.com/square/okhttp"},
                "sources": ["ecosyste.ms", "deps.dev"],
                "errors": [],
            },
        )
        rechecked = resolver.recheck_conflicts(limit=1, registry="maven")
        assert rechecked["resolved_conflicts"] == 1
        assert rechecked["by_status"] == {"AUTHORITY_VERIFIED": 1}
    finally:
        queue.close()
