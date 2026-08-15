from __future__ import annotations

from pathlib import Path

import pytest

from ivoiredata.technology_harvester import (
    HarvestCandidate,
    RegistryHarvester,
    TechnologyHarvestQueue,
    qualify_pending,
)
from ivoiredata.technology_registries import native_package_metadata


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses.pop(0)


def test_sqlite_queue_deduplicates_and_preserves_highest_priority(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        assert queue.upsert_many([HarvestCandidate("pypi.org", "Django", "seed", 10)]) == (1, 0)
        assert queue.upsert_many([HarvestCandidate("pypi.org", "Django", "updates", 80)]) == (0, 1)
        pending = queue.pending(10)
        assert len(pending) == 1
        assert pending[0]["priority"] == 80
        assert pending[0]["source"] == "updates"
        assert queue.audit()["candidates"] == 1
    finally:
        queue.close()


def test_ordinary_rediscovery_does_not_requeue_qualified_candidate(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        queue.upsert_many([HarvestCandidate("packagist.org", "laravel/framework", "popular", 90)])
        queue.mark_qualified("packagist.org", "laravel/framework")
        queue.upsert_many([HarvestCandidate("packagist.org", "laravel/framework", "popular", 90)])
        assert queue.pending(10) == []
        assert queue.audit()["by_status"] == {"QUALIFIED": 1}
    finally:
        queue.close()


def test_real_upstream_change_requeues_qualified_candidate(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        queue.upsert_many([HarvestCandidate("packagist.org", "laravel/framework", "popular", 90)])
        queue.mark_qualified("packagist.org", "laravel/framework")
        queue.upsert_many([HarvestCandidate("packagist.org", "laravel/framework", "changes", 90, requeue=True)])
        pending = queue.pending(10)
        assert len(pending) == 1
        assert pending[0]["status"] == "PENDING"
        assert pending[0]["source"] == "changes"
    finally:
        queue.close()


def test_packagist_popular_harvests_bounded_candidates(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    session = FakeSession([
        FakeResponse({
            "packages": [
                {"name": "laravel/framework", "downloads": 9000000, "favers": 5000},
                {"name": "symfony/console", "downloads": 8000000, "favers": 4000},
            ],
            "next": None,
        })
    ])
    try:
        result = RegistryHarvester(queue=queue, user_agent="test", session=session).harvest("packagist", limit=2)
        assert result["discovered"] == 2
        rows = queue.pending(10)
        assert [row["name"] for row in rows] == ["laravel/framework", "symfony/console"]
        assert all(row["priority"] >= 40 for row in rows)
    finally:
        queue.close()


def test_pubdev_harvester_persists_next_url_cursor(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    session = FakeSession([
        FakeResponse({"packages": ["http", "provider"], "nextUrl": "https://pub.dev/api/package-names?page=2"})
    ])
    try:
        result = RegistryHarvester(queue=queue, user_agent="test", session=session).harvest("pub", limit=2)
        assert result["discovered"] == 2
        assert queue.cursor("pubdev-package-names")["cursor"].endswith("page=2")
    finally:
        queue.close()


def test_pubdev_complete_cursor_prevents_restart_from_page_one(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    session = FakeSession([
        FakeResponse({"packages": ["http"], "nextUrl": None})
    ])
    try:
        harvester = RegistryHarvester(queue=queue, user_agent="test", session=session)
        first = harvester.harvest("pub", limit=100)
        assert first["complete"] is True
        assert queue.cursor("pubdev-package-names")["cursor"] == "__COMPLETE__"
        second = harvester.harvest("pub", limit=100)
        assert second["complete"] is True
        assert second["discovered"] == 0
        assert len(session.calls) == 1
    finally:
        queue.close()


def test_pypi_full_index_requires_explicit_flag(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        harvester = RegistryHarvester(queue=queue, user_agent="test", session=FakeSession([]))
        with pytest.raises(ValueError, match="explicit"):
            harvester.harvest("pypi", limit=10, full=False)
    finally:
        queue.close()


def test_pypi_full_index_uses_json_simple_api_and_serial(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    session = FakeSession([
        FakeResponse(
            {
                "meta": {"_last-serial": 12345, "api-version": "1.4"},
                "projects": [{"name": "Django"}, {"name": "FastAPI"}, {"name": "Flask"}],
            },
            headers={"ETag": '"abc"', "X-PyPI-Last-Serial": "12345"},
        )
    ])
    try:
        result = RegistryHarvester(queue=queue, user_agent="test", session=session).harvest("pypi", limit=2, full=True)
        assert result["discovered"] == 2
        assert queue.cursor("pypi-simple")["cursor"] == "12345"
        headers = session.calls[0][1]["headers"]
        assert headers["Accept"] == "application/vnd.pypi.simple.v1+json"
    finally:
        queue.close()


def test_nuget_project_url_is_not_promoted_to_repository():
    session = FakeSession([
        FakeResponse({
            "resources": [{"@type": "RegistrationsBaseUrl/3.6.0", "@id": "https://api.nuget.org/v3/registration5-gz-semver2/"}]
        }),
        FakeResponse({
            "items": [{
                "items": [{
                    "catalogEntry": {
                        "id": "Microsoft.EntityFrameworkCore",
                        "version": "10.0.11",
                        "listed": True,
                        "projectUrl": "https://learn.microsoft.com/ef/core/",
                        "readmeUrl": "https://www.nuget.org/packages/Microsoft.EntityFrameworkCore/10.0.11#readme-body-tab",
                    }
                }]
            }]
        }),
    ])
    row = native_package_metadata("nuget.org", "Microsoft.EntityFrameworkCore", session=session, user_agent="test")
    assert row is not None
    assert row["latest_stable_version"] == "10.0.11"
    assert row["canonical_repository"] is None
    assert row["official_website"] == "https://learn.microsoft.com/ef/core/"


class FakeCatalog:
    def __init__(self):
        self.calls = []

    def discover_package(self, registry, name):
        self.calls.append((registry, name))
        return {"registry": registry, "name": name}


def test_qualify_pending_moves_candidates_to_qualified(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    queue.upsert_many([
        HarvestCandidate("pypi.org", "Django", "seed", 90),
        HarvestCandidate("packagist.org", "laravel/framework", "seed", 80),
    ])
    catalog = FakeCatalog()
    try:
        result = qualify_pending(queue=queue, catalog_engine=catalog, limit=2)
        assert result == {"selected": 2, "success": 2, "failed": 0, "failures": []}
        assert queue.audit()["by_status"] == {"QUALIFIED": 2}
    finally:
        queue.close()
