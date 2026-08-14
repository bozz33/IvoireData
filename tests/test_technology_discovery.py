from __future__ import annotations

from pathlib import Path

from ivoiredata.technology_discovery import (
    GlobalTechnologyDiscoveryEngine,
    normalize_repository_url,
    normalize_registry,
    officiality_score,
    package_purl,
    parse_linguist_languages,
)


class FakeResponse:
    def __init__(self, *, payload=None, text=""):
        self._payload = payload
        self.text = text

    def raise_for_status(self):
        return None

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


def test_registry_and_repository_normalization():
    assert normalize_registry("npm") == "npmjs.org"
    assert normalize_registry("Cargo") == "crates.io"
    assert normalize_repository_url("git+https://github.com/facebook/react.git") == "https://github.com/facebook/react"
    assert normalize_repository_url("git@github.com:laravel/framework.git") == "https://github.com/laravel/framework"


def test_common_package_purls():
    assert package_purl("npm", "react") == "pkg:npm/react"
    assert package_purl("pypi", "Django", "5.2.1") == "pkg:pypi/Django@5.2.1"
    assert package_purl("packagist", "laravel/framework") == "pkg:composer/laravel/framework"


def test_officiality_requires_cross_source_evidence_for_verified():
    score, evidence = officiality_score(
        registry_repo="https://github.com/acme/framework",
        deps_repo="https://github.com/acme/framework.git",
        homepage="https://acme.dev",
        docs="https://acme.dev/docs",
        version="8.2.1",
    )
    assert score == 100
    assert "REGISTRY_REPOSITORY" in evidence
    assert "DEPS_DEV_REPOSITORY_MATCH" in evidence


def test_linguist_parser_keeps_programming_languages_only():
    payload = """
# comment
Python:
  type: programming
  language_id: 303
Markdown:
  type: prose
  language_id: 222
C++:
  type: programming
  group: C
  language_id: 43
"""
    rows = parse_linguist_languages(payload)
    assert [row["name"] for row in rows] == ["Python", "C++"]
    assert rows[1]["group"] == "C"


def test_package_discovery_cross_checks_ecosystems_and_deps_dev(tmp_path: Path):
    session = FakeSession([
        FakeResponse(payload={
            "name": "react",
            "latest_release_number": "19.1.1",
            "repository_url": "https://github.com/facebook/react.git",
            "homepage": "https://react.dev",
            "documentation_url": "https://react.dev/reference/react",
        }),
        FakeResponse(payload={
            "versions": [
                {"versionKey": {"version": "18.3.1"}, "isDefault": False},
                {"versionKey": {"version": "19.1.1"}, "isDefault": True},
            ]
        }),
        FakeResponse(payload={
            "links": [
                {"label": "SOURCE_REPO", "url": "https://github.com/facebook/react"},
                {"label": "HOMEPAGE", "url": "https://react.dev"},
            ]
        }),
    ])
    engine = GlobalTechnologyDiscoveryEngine(
        state_path=tmp_path / "technology_catalog.json",
        user_agent="IvoireData-test",
        session=session,
    )
    row = engine.discover_package("npm", "react")
    assert row["purl"] == "pkg:npm/react"
    assert row["latest_stable_version"] == "19.1.1"
    assert row["canonical_repository"] == "https://github.com/facebook/react"
    assert row["officiality_status"] == "VERIFIED_OFFICIAL"
    assert row["officiality_score"] == 100
    assert row["enabled_for_corpus"] is False
    assert (tmp_path / "technology_catalog.json").exists()


def test_language_discovery_never_auto_enables_corpus(tmp_path: Path):
    session = FakeSession([
        FakeResponse(text="Python:\n  type: programming\n  language_id: 303\nMarkdown:\n  type: prose\n"),
    ])
    engine = GlobalTechnologyDiscoveryEngine(
        state_path=tmp_path / "technology_catalog.json",
        user_agent="IvoireData-test",
        session=session,
    )
    rows = engine.discover_languages()
    assert len(rows) == 1
    assert rows[0]["name"] == "Python"
    assert rows[0]["enabled_for_corpus"] is False
    assert engine.audit()["by_category"] == {"LANGUAGE": 1}
