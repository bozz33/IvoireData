from __future__ import annotations

from ivoiredata.technology_harvester import TechnologyHarvestQueue
from ivoiredata.technology_nuget import NuGetCatalogHarvester
from ivoiredata.technology_registries import native_package_metadata


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
            value = value.pop(0)
        return FakeResponse(value)


def _service(catalog):
    return {"resources": [{"@id": catalog, "@type": "Catalog/3.0.0"}]}


def _leaf(pid, version, ts, kind="PackageDetails"):
    return {
        "@id": f"https://api.nuget.test/leaf/{pid.lower()}/{version}/{kind}.json",
        "@type": f"nuget:{kind}",
        "commitTimeStamp": ts,
        "commitId": "c-" + ts,
        "nuget:id": pid,
        "nuget:version": version,
    }


def _index(ts, page):
    return {
        "commitTimeStamp": ts,
        "commitId": "idx-" + ts,
        "count": 1,
        "items": [{"@id": page, "commitTimeStamp": ts, "count": 10}],
    }


def _page(ts, items):
    return {"commitTimeStamp": ts, "count": len(items), "items": items}


def test_nuget_follower_deleted_packages_is_delta_not_registry_total(tmp_path):
    catalog = "https://api.nuget.test/catalog/index.json"
    page = "https://api.nuget.test/catalog/page.json"
    base = "2026-08-15T10:00:00Z"
    target = "2026-08-15T11:00:00Z"

    base_items = [
        _leaf("Alive", "1.0.0", "2026-08-15T09:57:00Z"),
        _leaf("Already.Dead", "1.0.0", "2026-08-15T09:58:00Z"),
        _leaf("Already.Dead", "1.0.0", "2026-08-15T09:59:00Z", "PackageDelete"),
    ]
    target_items = base_items + [_leaf("New.Package", "1.0.0", target)]
    session = FakeSession({
        "https://api.nuget.org/v3/index.json": _service(catalog),
        catalog: [_index(base, page), _index(target, page)],
        page: [_page(base, base_items), _page(target, target_items)],
    })
    queue = TechnologyHarvestQueue(tmp_path / "queue.sqlite3")
    try:
        harvester = NuGetCatalogHarvester(queue=queue, user_agent="test", session=session)
        boot = harvester.bootstrap(limit=0, reset=True)
        assert boot["complete"] is True
        assert boot["registry_deleted_packages"] == 1

        delta = harvester.changes(limit=100)
        assert delta["processed_items"] == 1
        assert delta["inserted_packages"] == 1
        assert delta["deleted_packages"] == 0
        assert delta["registry_deleted_packages"] == 1
    finally:
        queue.close()


def test_nuget_latest_stable_rejects_any_prerelease_suffix():
    service_url = "https://api.nuget.org/v3/index.json"
    registration = "https://api.nuget.test/registration/"
    package_url = registration + "serilog/index.json"
    session = FakeSession({
        service_url: {
            "resources": [{"@id": registration, "@type": "RegistrationsBaseUrl/3.6.0"}]
        },
        package_url: {
            "items": [{
                "items": [
                    {"catalogEntry": {"id": "Serilog", "version": "4.4.0", "listed": True}},
                    {"catalogEntry": {"id": "Serilog", "version": "4.4.1-dependabot-02442", "listed": True}},
                    {"catalogEntry": {"id": "Serilog", "version": "4.4.1-zzz", "listed": True}},
                    {"catalogEntry": {"id": "Serilog", "version": "4.4.0+build.9", "listed": True}},
                    {"catalogEntry": {"id": "Serilog", "version": "5.0.0-beta.1", "listed": True}},
                    {"catalogEntry": {"id": "Serilog", "version": "99.0.0", "listed": False}},
                ]
            }]
        },
    })
    metadata = native_package_metadata("nuget.org", "Serilog", session=session, user_agent="test")
    assert metadata is not None
    assert metadata["latest_stable_version"] in {"4.4.0", "4.4.0+build.9"}
    assert "-" not in metadata["latest_stable_version"].split("+", 1)[0]
