from __future__ import annotations

from pathlib import Path

from ivoiredata.technology_catalog import GlobalTechnologyCatalogEngine
from ivoiredata.technology_registries import build_purl, importance_score, native_package_metadata


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
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if not self.payloads:
            raise AssertionError(f"unexpected request: {url}")
        return FakeResponse(self.payloads.pop(0))


def test_purl_handles_scoped_npm_and_maven_coordinates():
    assert build_purl("npm", "@angular/core", "20.1.0") == "pkg:npm/%40angular/core@20.1.0"
    assert build_purl("maven", "org.springframework:spring-core", "7.0.1") == "pkg:maven/org.springframework/spring-core@7.0.1"


def test_native_pypi_metadata_extracts_repo_docs_and_version():
    session = FakeSession([{
        "info": {
            "name": "Django",
            "version": "5.2.7",
            "home_page": "https://www.djangoproject.com/",
            "project_urls": {
                "Source": "https://github.com/django/django",
                "Documentation": "https://docs.djangoproject.com/",
            },
        }
    }])
    row = native_package_metadata("pypi.org", "Django", session=session, user_agent="test")
    assert row is not None
    assert row["authority_source"] == "pypi"
    assert row["latest_stable_version"] == "5.2.7"
    assert row["canonical_repository"] == "https://github.com/django/django"
    assert row["documentation_url"] == "https://docs.djangoproject.com/"


def test_phase2_package_prefers_native_registry_then_cross_checks(tmp_path: Path):
    session = FakeSession([
        # npm native registry
        {
            "name": "react",
            "dist-tags": {"latest": "19.1.1"},
            "versions": {
                "19.1.1": {
                    "repository": {"type": "git", "url": "git+https://github.com/facebook/react.git"},
                    "homepage": "https://react.dev/",
                }
            },
        },
        # ecosyste.ms enrichment
        {
            "name": "react",
            "latest_release_number": "19.1.1",
            "repository_url": "https://github.com/facebook/react",
            "documentation_url": "https://react.dev/reference/react",
            "dependent_repos_count": 500000,
            "repository_stars": 240000,
        },
        # deps.dev package
        {
            "versions": [{"versionKey": {"version": "19.1.1"}, "isDefault": True}],
        },
        # deps.dev version
        {
            "links": [
                {"label": "SOURCE_REPO", "url": "https://github.com/facebook/react"},
                {"label": "HOMEPAGE", "url": "https://react.dev"},
            ]
        },
    ])
    engine = GlobalTechnologyCatalogEngine(
        state_path=tmp_path / "technology_catalog.json",
        user_agent="IvoireData-test",
        session=session,
    )
    row = engine.discover_package("npm", "react")
    assert session.calls[0].startswith("https://registry.npmjs.org/")
    assert row["authority_source"] == "npm"
    assert row["latest_stable_version"] == "19.1.1"
    assert row["canonical_repository"] == "https://github.com/facebook/react"
    assert row["officiality_status"] == "VERIFIED_OFFICIAL"
    assert "NATIVE_REGISTRY_METADATA" in row["officiality_evidence"]
    assert row["importance_score"] > 0
    assert row["enabled_for_corpus"] is False


def test_identity_reconciliation_links_wikidata_to_single_package(tmp_path: Path):
    engine = GlobalTechnologyCatalogEngine(state_path=tmp_path / "catalog.json", user_agent="test")
    engine.data["technologies"] = {
        "pkg:pypi/Django": {
            "technology_id": "pkg:pypi/Django",
            "name": "Django",
            "purl": "pkg:pypi/Django",
            "canonical_repository": "https://github.com/django/django",
            "officiality_score": 100,
            "enabled_for_corpus": False,
            "discovery_sources": ["pypi"],
        },
        "wikidata:Q842014": {
            "technology_id": "wikidata:Q842014",
            "name": "Django",
            "qid": "Q842014",
            "canonical_repository": "https://github.com/django/django.git",
            "documentation_url": "https://docs.djangoproject.com/",
            "officiality_score": 80,
            "enabled_for_corpus": False,
            "discovery_sources": ["wikidata"],
        },
    }
    result = engine.reconcile_identities()
    assert result["groups"] == 1
    assert result["merged_aliases"] == 1
    package = engine.data["technologies"]["pkg:pypi/Django"]
    alias = engine.data["technologies"]["wikidata:Q842014"]
    assert package["identity_status"] == "CANONICAL"
    assert package["documentation_url"] == "https://docs.djangoproject.com/"
    assert package["wikidata_qids"] == ["Q842014"]
    assert alias["canonical_technology_id"] == "pkg:pypi/Django"


def test_identity_reconciliation_does_not_collapse_monorepo_packages(tmp_path: Path):
    engine = GlobalTechnologyCatalogEngine(state_path=tmp_path / "catalog.json", user_agent="test")
    repo = "https://github.com/facebook/react"
    engine.data["technologies"] = {
        "pkg:npm/react": {"canonical_repository": repo, "officiality_score": 100},
        "pkg:npm/react-dom": {"canonical_repository": repo, "officiality_score": 100},
        "wikidata:Q19399674": {"canonical_repository": repo, "officiality_score": 80},
    }
    result = engine.reconcile_identities()
    assert result["monorepo_ambiguous_groups"] == 1
    assert engine.data["technologies"]["pkg:npm/react"]["identity_status"] == "MONOREPO_AMBIGUOUS"
    assert "canonical_technology_id" not in engine.data["technologies"]["pkg:npm/react-dom"]


def test_importance_score_is_ranking_only():
    score, tier = importance_score({
        "officiality_score": 100,
        "downloads_total": 1_000_000_000,
        "downloads_recent": 100_000_000,
        "dependents_count": 100_000,
        "repository_stars": 100_000,
    })
    assert score >= 80
    assert tier == "CORE"
