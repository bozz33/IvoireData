from __future__ import annotations

from pathlib import Path

from ivoiredata.technology_harvester import TechnologyHarvestQueue
from ivoiredata.technology_nuget import (
    NUGET_BOOTSTRAP_SOURCE,
    NUGET_CHANGES_SOURCE,
    NuGetCatalogHarvester,
)
from ivoiredata.technology_registries import build_purl, native_package_metadata


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.headers = {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.routes[url]
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"no fake responses left for {url}")
            value = value.pop(0)
        return FakeResponse(value)


def _service(catalog_url="https://api.nuget.test/catalog/index.json"):
    return {
        "version": "3.0.0",
        "resources": [
            {"@id": catalog_url, "@type": "Catalog/3.0.0"},
            {"@id": "https://api.nuget.test/registration/", "@type": "RegistrationsBaseUrl/3.6.0"},
        ],
    }


def _leaf(pid, version, ts, *, kind="PackageDetails", suffix="x"):
    return {
        "@id": f"https://api.nuget.test/leaf/{pid.lower()}/{version}/{suffix}.json",
        "@type": f"nuget:{kind}",
        "commitId": f"commit-{ts}",
        "commitTimeStamp": ts,
        "nuget:id": pid,
        "nuget:version": version,
    }


def _index(ts, pages):
    return {
        "commitId": f"index-{ts}",
        "commitTimeStamp": ts,
        "count": len(pages),
        "items": [
            {
                "@id": url,
                "commitId": f"page-{i}",
                "commitTimeStamp": page_ts,
                "count": count,
            }
            for i, (url, page_ts, count) in enumerate(pages)
        ],
    }


def _page(ts, items):
    return {"commitId": f"page-{ts}", "commitTimeStamp": ts, "count": len(items), "items": items}


def test_nuget_follower_refuses_fake_head_before_bootstrap(tmp_path):
    queue = TechnologyHarvestQueue(tmp_path / "queue.sqlite3")
    session = FakeSession({})
    try:
        harvester = NuGetCatalogHarvester(queue=queue, user_agent="test", session=session)
        result = harvester.changes(limit=10)
        assert result["bootstrap_required"] is True
        assert result["processed_items"] == 0
        assert session.calls == []
        assert queue.cursor(NUGET_CHANGES_SOURCE) == {}
    finally:
        queue.close()


def test_nuget_bootstrap_is_bounded_resumable_and_version_delete_safe(tmp_path):
    catalog = "https://api.nuget.test/catalog/index.json"
    p0 = "https://api.nuget.test/catalog/page0.json"
    p1 = "https://api.nuget.test/catalog/page1.json"
    snapshot = "2026-08-15T10:00:00.0000000Z"
    pages = [(p0, "2026-08-15T09:00:00.0000000Z", 3), (p1, snapshot, 3)]
    index = _index(snapshot, pages)
    page0 = _page(
        pages[0][1],
        [
            _leaf("Alpha", "1.0.0", "2026-08-15T08:00:00.0000000Z", suffix="a1"),
            _leaf("Alpha", "2.0.0", "2026-08-15T08:10:00.0000000Z", suffix="a2"),
            _leaf("Dead.One", "1.0.0", "2026-08-15T08:20:00.0000000Z", suffix="d1"),
        ],
    )
    page1 = _page(
        snapshot,
        [
            _leaf("Alpha", "1.0.0", "2026-08-15T09:10:00.0000000Z", kind="PackageDelete", suffix="ad"),
            _leaf("Dead.One", "1.0.0", "2026-08-15T09:20:00.0000000Z", kind="PackageDelete", suffix="dd"),
            _leaf("Gamma", "1.0.0", snapshot, suffix="g1"),
        ],
    )
    session = FakeSession(
        {
            "https://api.nuget.org/v3/index.json": _service(catalog),
            catalog: [index, index, index],
            p0: [page0, page0],
            p1: [page1, page1],
        }
    )
    queue = TechnologyHarvestQueue(tmp_path / "queue.sqlite3")
    try:
        harvester = NuGetCatalogHarvester(queue=queue, user_agent="test", session=session)

        first = harvester.bootstrap(limit=2, reset=True)
        assert first["complete"] is False
        assert first["processed_items"] == 2
        assert first["snapshot_timestamp"] == snapshot
        assert first["changes_cursor"] is None
        first_state = queue.cursor(NUGET_BOOTSTRAP_SOURCE)["cursor"]

        second = harvester.bootstrap(limit=2)
        assert second["complete"] is False
        assert second["snapshot_timestamp"] == snapshot
        assert second["processed_items"] == 2
        assert queue.cursor(NUGET_BOOTSTRAP_SOURCE)["cursor"] != first_state
        assert queue.cursor(NUGET_CHANGES_SOURCE) == {}

        final = harvester.bootstrap(limit=0)
        assert final["complete"] is True
        assert final["snapshot_timestamp"] == snapshot
        assert final["changes_cursor"] == snapshot
        assert final["active_packages"] == 2  # Alpha + Gamma
        assert final["deleted_packages"] == 1  # Dead.One
        assert final["version_states"] == 4  # Alpha x2 + Dead.One + Gamma
        assert final["deleted_versions"] == 2

        alpha = queue.db.execute(
            "SELECT status FROM candidates WHERE registry='nuget.org' AND lower(name)='alpha'"
        ).fetchone()
        dead = queue.db.execute(
            "SELECT status FROM candidates WHERE registry='nuget.org' AND lower(name)='dead.one'"
        ).fetchone()
        assert alpha["status"] == "PENDING"  # deleting 1.0 must not delete Alpha 2.0
        assert dead["status"] == "DELETED"

        before_calls = len(session.calls)
        rerun = harvester.bootstrap(limit=500)
        assert rerun["complete"] is True
        assert rerun["processed_items"] == 0
        assert rerun["http_work_required"] is False
        assert len(session.calls) == before_calls
    finally:
        queue.close()


def test_nuget_incremental_cursor_resumes_inside_same_commit_without_loss(tmp_path):
    catalog = "https://api.nuget.test/catalog/index.json"
    p0 = "https://api.nuget.test/catalog/page0.json"
    base = "2026-08-15T10:00:00.0000000Z"
    target = "2026-08-15T11:00:00.0000000Z"
    base_index = _index(base, [(p0, base, 1)])
    target_index = _index(target, [(p0, target, 3)])
    base_page = _page(base, [_leaf("Alpha", "1.0.0", base, suffix="base")])
    # Two leaves deliberately share the same commit timestamp. A limit=1 run must not
    # advance cursor_timestamp to target and lose the second leaf.
    target_page = _page(
        target,
        [
            _leaf("Alpha", "2.0.0", target, suffix="alpha2"),
            _leaf("Beta", "1.0.0", target, suffix="beta1"),
            _leaf("Gone", "1.0.0", target, kind="PackageDelete", suffix="gone-delete"),
        ],
    )
    session = FakeSession(
        {
            "https://api.nuget.org/v3/index.json": _service(catalog),
            catalog: [base_index, target_index, target_index],
            p0: [base_page, target_page, target_page],
        }
    )
    queue = TechnologyHarvestQueue(tmp_path / "queue.sqlite3")
    try:
        harvester = NuGetCatalogHarvester(queue=queue, user_agent="test", session=session)
        boot = harvester.bootstrap(limit=0, reset=True)
        assert boot["complete"] is True
        assert boot["changes_cursor"] == base

        partial = harvester.changes(limit=1)
        assert partial["target_complete"] is False
        assert partial["previous_cursor"] == base
        assert partial["cursor"] == base
        assert partial["inflight"] is not None
        assert partial["processed_items"] == 1

        finish = harvester.changes(limit=10)
        assert finish["target_complete"] is True
        assert finish["previous_cursor"] == base
        assert finish["cursor"] == target
        assert finish["inflight"] is None
        assert finish["processed_items"] == 2

        beta = queue.db.execute(
            "SELECT status FROM candidates WHERE registry='nuget.org' AND lower(name)='beta'"
        ).fetchone()
        assert beta is not None and beta["status"] == "PENDING"
    finally:
        queue.close()


def test_nuget_native_authority_uses_registration_resource():
    service_url = "https://api.nuget.org/v3/index.json"
    registration = "https://api.nuget.test/registration/"
    package_url = registration + "newtonsoft.json/index.json"
    session = FakeSession(
        {
            service_url: {
                "resources": [
                    {"@id": registration, "@type": "RegistrationsBaseUrl/3.6.0"},
                ]
            },
            package_url: {
                "items": [
                    {
                        "items": [
                            {
                                "catalogEntry": {
                                    "id": "Newtonsoft.Json",
                                    "version": "13.0.3",
                                    "listed": True,
                                    "projectUrl": "https://www.newtonsoft.com/json",
                                    "repository": "https://github.com/JamesNK/Newtonsoft.Json.git",
                                }
                            }
                        ]
                    }
                ]
            },
        }
    )
    metadata = native_package_metadata(
        "nuget.org",
        "Newtonsoft.Json",
        session=session,
        user_agent="test",
    )
    assert metadata is not None
    assert metadata["authority_source"] == "nuget"
    assert metadata["name"] == "Newtonsoft.Json"
    assert metadata["latest_stable_version"] == "13.0.3"
    assert metadata["canonical_repository"] == "https://github.com/JamesNK/Newtonsoft.Json"
    assert build_purl("nuget.org", metadata["name"]) == "pkg:nuget/Newtonsoft.Json"
