from __future__ import annotations

from ivoiredata.technology_registries import native_package_metadata


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url, **kwargs):
        return FakeResponse(self.payload)


def test_explicit_pypi_source_url_can_use_non_github_vcs_host():
    row = native_package_metadata(
        "pypi.org",
        "example",
        session=FakeSession({
            "info": {
                "name": "example",
                "version": "1.2.3",
                "project_urls": {
                    "Source": "https://codeberg.org/acme/example",
                    "Documentation": "https://docs.example.org/",
                },
            }
        }),
        user_agent="test",
    )
    assert row is not None
    assert row["canonical_repository"] == "https://codeberg.org/acme/example"
    assert row["documentation_url"] == "https://docs.example.org/"
