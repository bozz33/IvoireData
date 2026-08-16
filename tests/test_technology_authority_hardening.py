from __future__ import annotations

from pathlib import Path

from ivoiredata.technology_authority import OfficialAuthorityResolver
from ivoiredata.technology_harvester import HarvestCandidate, TechnologyHarvestQueue
from ivoiredata.technology_qualification import TechnologyQualificationEngine


def _native(name: str) -> dict:
    return {
        "authority_source": "npm",
        "native_registry_url": f"https://registry.npmjs.org/{name}",
        "name": name,
        "latest_stable_version": "1.0.0",
        "canonical_repository": f"https://github.com/example/{name}",
        "documentation_url": f"https://docs.example.test/{name}",
        "official_website": f"https://example.test/{name}",
        "downloads_total": 100_000_000,
        "downloads_recent": 10_000_000,
        "dependents_count": 100_000,
    }


def _crosscheck(row: dict, native: dict) -> dict:
    return {
        "ecosystems": {"repository_url": native["canonical_repository"]},
        "deps_package": {},
        "deps_version": {},
        "deps_links": {},
        "sources": ["ecosyste.ms"],
        "errors": [],
    }


def test_same_timestamp_real_requalification_invalidates_authority_decision(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    calls = 0

    def native_resolver(registry: str, name: str):
        nonlocal calls
        calls += 1
        return _native(name)

    try:
        queue.upsert_many([HarvestCandidate("npmjs.org", "project", "seed", 95)])
        qualifier = TechnologyQualificationEngine(
            queue=queue,
            user_agent="test",
            native_resolver=native_resolver,
        )
        assert qualifier.run(limit=1, registry="npm")["ready_for_authority"] == 1

        authority_calls = 0

        def authority_crosscheck(row: dict, native: dict):
            nonlocal authority_calls
            authority_calls += 1
            return _crosscheck(row, native)

        authority = OfficialAuthorityResolver(
            queue=queue,
            user_agent="test",
            crosscheck_resolver=authority_crosscheck,
        )
        assert authority.run(limit=1, registry="npm")["verified"] == 1
        assert authority_calls == 1

        previous = queue.db.execute(
            "SELECT qualification_checked_at FROM authority_results WHERE registry='npmjs.org' AND name='project'"
        ).fetchone()["qualification_checked_at"]

        queue.upsert_many([
            HarvestCandidate("npmjs.org", "project", "npm-changes", 95, requeue=True)
        ])
        assert qualifier.run(limit=1, registry="npm")["ready_for_authority"] == 1
        assert calls == 2

        # Force the exact collision that a seconds-resolution clock can produce.
        with queue.db:
            queue.db.execute(
                "UPDATE qualification_results SET last_checked_at=? WHERE registry='npmjs.org' AND name='project'",
                (previous,),
            )

        refreshed = authority.run(limit=1, registry="npm")
        assert refreshed["verified"] == 1
        assert authority_calls == 2
    finally:
        queue.close()


def test_verified_ready_for_docs_is_total_not_top_window(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        queue.upsert_many([
            HarvestCandidate("npmjs.org", "one", "seed", 95),
            HarvestCandidate("npmjs.org", "two", "seed", 95),
        ])
        qualifier = TechnologyQualificationEngine(
            queue=queue,
            user_agent="test",
            native_resolver=lambda registry, name: _native(name),
        )
        result = qualifier.run(limit=2, registry="npm")
        assert result["ready_for_authority"] == 2

        authority = OfficialAuthorityResolver(
            queue=queue,
            user_agent="test",
            crosscheck_resolver=_crosscheck,
        )
        assert authority.run(limit=2, registry="npm")["verified"] == 2

        audit = authority.audit(top=1)
        assert audit["verified_ready_for_docs"] == 2
        assert len(audit["top_verified"]) == 1
    finally:
        queue.close()
