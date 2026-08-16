from __future__ import annotations

from pathlib import Path

from ivoiredata.technology_authority import OfficialAuthorityResolver
from ivoiredata.technology_harvester import HarvestCandidate, TechnologyHarvestQueue
from ivoiredata.technology_qualification import TechnologyQualificationEngine


def _native(name: str, *, repository: str = "https://github.com/example/project") -> dict:
    return {
        "authority_source": "native-registry",
        "native_registry_url": f"https://registry.example.test/{name}",
        "name": name,
        "latest_stable_version": "1.2.3",
        "canonical_repository": repository,
        "documentation_url": "https://docs.example.test/project",
        "official_website": "https://example.test/project",
        "downloads_total": 100_000_000,
        "downloads_recent": 10_000_000,
        "dependents_count": 100_000,
    }


def _qualify(queue: TechnologyHarvestQueue, *, registry: str = "npmjs.org", name: str = "example") -> int:
    calls = 0

    def resolver(_registry: str, package: str):
        nonlocal calls
        calls += 1
        return _native(package)

    queue.upsert_many([HarvestCandidate(registry, name, "seed", 95)])
    qualifier = TechnologyQualificationEngine(
        queue=queue,
        user_agent="test",
        native_resolver=resolver,
    )
    result = qualifier.run(limit=1, registry=registry)
    assert result["ready_for_authority"] == 1
    return calls


def test_authority_reuses_native_metadata_and_only_crosschecks_promoted_candidate(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        assert _qualify(queue) == 1
        seen_native: list[dict] = []

        def crosscheck(row: dict, native: dict):
            seen_native.append(native)
            assert row["qualification_status"] == "READY_FOR_AUTHORITY"
            return {
                "ecosystems": {"repository_url": "https://github.com/example/project"},
                "deps_package": {},
                "deps_version": {},
                "deps_links": {},
                "sources": ["ecosyste.ms"],
                "errors": [],
            }

        authority = OfficialAuthorityResolver(
            queue=queue,
            user_agent="test",
            crosscheck_resolver=crosscheck,
        )
        result = authority.run(limit=1)
        assert result["verified"] == 1
        assert result["outcomes"][0]["status"] == "AUTHORITY_VERIFIED"
        assert result["outcomes"][0]["repository_match"] is True
        assert seen_native[0]["native_registry_url"].startswith("https://registry.example.test/")

        # The authority stage has its own durable decision. Running it again with no
        # upstream change performs zero cross-check work.
        second = authority.run(limit=1)
        assert second["selected"] == 0
        assert len(seen_native) == 1
    finally:
        queue.close()


def test_repository_conflict_blocks_automatic_verification(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _qualify(queue)
        authority = OfficialAuthorityResolver(
            queue=queue,
            user_agent="test",
            crosscheck_resolver=lambda row, native: {
                "ecosystems": {"repository_url": "https://github.com/attacker/other"},
                "deps_package": {},
                "deps_version": {},
                "deps_links": {},
                "sources": ["ecosyste.ms"],
                "errors": [],
            },
        )
        result = authority.run(limit=1)
        assert result["conflicts"] == 1
        outcome = result["outcomes"][0]
        assert outcome["status"] == "AUTHORITY_CONFLICT"
        assert outcome["repository_conflict"] is True
        assert int(outcome["officiality_score"]) <= 79
    finally:
        queue.close()


def test_transient_crosscheck_gap_uses_persisted_backoff(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    calls = 0
    try:
        _qualify(queue)

        def crosscheck(row: dict, native: dict):
            nonlocal calls
            calls += 1
            return {
                "ecosystems": {},
                "deps_package": {},
                "deps_version": {},
                "deps_links": {},
                "sources": [],
                "errors": ["ecosyste.ms: timeout", "deps.dev: timeout"],
            }

        authority = OfficialAuthorityResolver(
            queue=queue,
            user_agent="test",
            crosscheck_resolver=crosscheck,
            retry_base_seconds=3600,
        )
        first = authority.run(limit=1)
        assert first["retry"] == 1
        assert first["outcomes"][0]["next_retry_at"]
        assert calls == 1

        second = authority.run(limit=1)
        assert second["selected"] == 0
        assert calls == 1
    finally:
        queue.close()


def test_probable_authority_is_not_sent_to_documentation_automatically(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _qualify(queue)
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
        result = authority.run(limit=1)
        assert result["probable"] == 1
        assert result["outcomes"][0]["status"] == "AUTHORITY_PROBABLE"
        assert result["outcomes"][0]["next_action"] == "AUTHORITY_REVIEW"
    finally:
        queue.close()


def test_authority_audit_lists_only_verified_ready_for_docs(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _qualify(queue, name="verified-package")
        authority = OfficialAuthorityResolver(
            queue=queue,
            user_agent="test",
            crosscheck_resolver=lambda row, native: {
                "ecosystems": {"repository_url": "https://github.com/example/project"},
                "deps_package": {},
                "deps_version": {},
                "deps_links": {},
                "sources": ["ecosyste.ms"],
                "errors": [],
            },
        )
        authority.run(limit=1)
        audit = authority.audit(top=10)
        assert audit["schema_version"] == 1
        assert audit["by_status"] == {"AUTHORITY_VERIFIED": 1}
        assert audit["verified_ready_for_docs"] == 1
        assert audit["top_verified"][0]["name"] == "verified-package"
    finally:
        queue.close()


def test_unlimited_authority_resolution_is_rejected(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _qualify(queue)
        authority = OfficialAuthorityResolver(
            queue=queue,
            user_agent="test",
            crosscheck_resolver=lambda row, native: {},
        )
        try:
            authority.run(limit=0)
        except ValueError as exc:
            assert "intentionally bounded" in str(exc)
        else:
            raise AssertionError("limit=0 must be rejected")
    finally:
        queue.close()
