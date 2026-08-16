from __future__ import annotations

from pathlib import Path

from ivoiredata.technology_harvester import HarvestCandidate, TechnologyHarvestQueue
from ivoiredata.technology_qualification import TechnologyQualificationEngine


def test_sleeping_retry_window_cannot_make_registry_look_exhausted(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    queue.upsert_many(
        [
            HarvestCandidate("npmjs.org", f"a{i:03d}", "seed", 15)
            for i in range(80)
        ]
        + [HarvestCandidate("npmjs.org", "z-eligible", "seed", 15)]
    )

    calls: list[str] = []

    def resolver(registry: str, name: str):
        calls.append(name)
        return {
            "authority_source": "npm",
            "native_registry_url": f"https://registry.npmjs.org/{name}",
            "name": name,
            "latest_stable_version": "1.0.0",
            "canonical_repository": "https://github.com/example/project",
        }

    try:
        engine = TechnologyQualificationEngine(
            queue=queue,
            user_agent="test",
            native_resolver=resolver,
        )
        # Simulate a large contiguous block of transient failures whose backoff has
        # not elapsed yet. An implementation that fetches a fixed window and filters
        # backoff in Python will incorrectly stop before reaching z-eligible.
        with queue.db:
            for i in range(80):
                name = f"a{i:03d}"
                queue.db.execute(
                    "UPDATE candidates SET status='RETRY',attempts=1 WHERE registry=? AND name=?",
                    ("npmjs.org", name),
                )
                queue.db.execute(
                    """
                    INSERT INTO qualification_results(
                        registry,name,qualification_status,first_checked_at,last_checked_at,next_retry_at
                    ) VALUES(?,?,'RETRY','2026-08-16T00:00:00Z','2026-08-16T00:00:00Z','2099-01-01T00:00:00Z')
                    """,
                    ("npmjs.org", name),
                )

        result = engine.run(limit=1, registry="npm")
        assert result["selected"] == 1
        assert result["processed"] == 1
        assert calls == ["z-eligible"]
    finally:
        queue.close()
