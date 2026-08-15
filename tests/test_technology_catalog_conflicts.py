from pathlib import Path

from ivoiredata.technology_catalog import GlobalTechnologyCatalogEngine


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    def get(self, url, **kwargs):
        if not self.payloads:
            raise AssertionError(f"unexpected request: {url}")
        return FakeResponse(self.payloads.pop(0))


def test_repository_conflict_never_verifies_official(tmp_path: Path):
    session = FakeSession([
        {
            "name": "example",
            "dist-tags": {"latest": "1.0.0"},
            "versions": {
                "1.0.0": {
                    "repository": {"url": "https://github.com/acme/official.git"},
                    "homepage": "https://example.dev",
                }
            },
        },
        {
            "name": "example",
            "latest_release_number": "1.0.0",
            "repository_url": "https://github.com/acme/official",
            "documentation_url": "https://example.dev/docs",
        },
        {"versions": [{"versionKey": {"version": "1.0.0"}, "isDefault": True}]},
        {"links": [{"label": "SOURCE_REPO", "url": "https://github.com/other/conflict"}]},
    ])
    engine = GlobalTechnologyCatalogEngine(
        state_path=tmp_path / "catalog.json",
        user_agent="test",
        session=session,
    )
    row = engine.discover_package("npm", "example")
    assert row["officiality_score"] <= 79
    assert row["officiality_status"] != "VERIFIED_OFFICIAL"
    assert "REPOSITORY_CONFLICT" in row["officiality_evidence"]
    assert row["alternate_repository"] == "https://github.com/other/conflict"
