from __future__ import annotations

from ivoiredata.technology_registries import build_purl, native_package_metadata


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_crates_io_api_is_native_authority_and_builds_cargo_purl():
    session = FakeSession(
        FakeResponse(
            {
                "crate": {
                    "id": "serde",
                    "name": "serde",
                    "max_stable_version": "1.0.228",
                    "max_version": "1.0.228",
                    "repository": "https://github.com/serde-rs/serde",
                    "documentation": "https://docs.rs/serde",
                    "homepage": "https://serde.rs",
                    "downloads": 123456789,
                    "recent_downloads": 123456,
                }
            }
        )
    )

    row = native_package_metadata(
        "crates.io",
        "serde",
        session=session,
        user_agent="IvoireData-test",
    )

    assert row is not None
    assert row["authority_source"] == "crates.io"
    assert row["name"] == "serde"
    assert row["latest_stable_version"] == "1.0.228"
    assert row["canonical_repository"] == "https://github.com/serde-rs/serde"
    assert row["documentation_url"] == "https://docs.rs/serde"
    assert row["official_website"] == "https://serde.rs"
    assert row["native_registry_url"] == "https://crates.io/api/v1/crates/serde"
    assert build_purl("crates.io", "serde") == "pkg:cargo/serde"
    assert build_purl("crates.io", "serde", "1.0.228") == "pkg:cargo/serde@1.0.228"
    assert session.calls[0][0] == "https://crates.io/api/v1/crates/serde"
