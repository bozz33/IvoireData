from __future__ import annotations

import time

import pytest

from ivoiredata.connectors import official_docs_strategy as strategy
from ivoiredata.connectors.official_git_docs import _path_allowed
from ivoiredata.connectors.official_git_hardening import (
    GitHubRateLimitError,
    _fetch_blob_body,
    _is_rate_limited,
    _retry_after_seconds,
    hardened_official_docs_resource,
    parse_github_tree_target,
)


def test_parse_github_tree_target_preserves_docs_subdirectory():
    assert parse_github_tree_target(
        "https://github.com/FasterXML/jackson-databind/tree/3.x/docs"
    ) == ("FasterXML/jackson-databind", "3.x", "docs/")
    assert parse_github_tree_target(
        "https://github.com/junit-team/junit4/tree/main/doc"
    ) == ("junit-team/junit4", "main", "doc/")
    assert parse_github_tree_target(
        "https://github.com/laravel/docs/tree/13.x"
    ) == ("laravel/docs", "13.x", None)


def test_canonical_tree_strategy_passes_exact_discovered_prefix(monkeypatch):
    called = {}

    def fake_git_resource(**kwargs):
        called.update(kwargs)
        return "git-resource"

    monkeypatch.setattr(strategy, "_git_resource", fake_git_resource)
    result = hardened_official_docs_resource(
        source_id="techdocs-maven-jackson",
        url="https://github.com/FasterXML/jackson-databind/tree/3.x/docs",
        include_prefixes=[],
        metadata_base={
            "source_strategy": "OFFICIAL_GIT",
            "public_docs_url": "https://github.com/FasterXML/jackson-databind/tree/3.x/docs",
        },
    )

    assert result == "git-resource"
    assert called["repository"] == "FasterXML/jackson-databind"
    assert called["ref"] == "3.x"
    assert called["include_prefixes"] == ["docs/"]


def test_scoped_path_filter_excludes_repository_root_markdown():
    prefixes = ["docs/"]
    assert _path_allowed("docs/README.md", prefixes, [])
    assert _path_allowed("docs/databind-guide.md", prefixes, [])
    assert not _path_allowed("CLAUDE.md", prefixes, [])
    assert not _path_allowed(".github/ISSUE_TEMPLATE/bug.md", prefixes, [])
    assert not _path_allowed("README.md", prefixes, [])


class _Response:
    def __init__(self, *, status_code=200, content=b"body", headers=None, text=""):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_authenticated_blob_fetch_uses_official_git_blob_rest_endpoint():
    session = _Session(_Response(content=b"# docs"))
    raw, url, transport = _fetch_blob_body(
        session,
        owner_repo="acme/project",
        commit="commit-sha",
        path="docs/start.md",
        blob_sha="blob-sha",
        api_headers={
            "User-Agent": "test",
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer secret",
        },
        user_agent="test",
    )

    assert raw == b"# docs"
    assert url == "https://api.github.com/repos/acme/project/git/blobs/blob-sha"
    assert transport == "GITHUB_GIT_BLOB_API_AUTHENTICATED"
    assert len(session.calls) == 1
    called_url, kwargs = session.calls[0]
    assert called_url.startswith("https://api.github.com/")
    assert "raw.githubusercontent.com" not in called_url
    assert kwargs["headers"]["Authorization"] == "Bearer secret"
    assert kwargs["headers"]["Accept"] == "application/vnd.github.raw+json"


def test_anonymous_blob_fetch_keeps_public_raw_transport():
    session = _Session(_Response(content=b"# docs"))
    raw, url, transport = _fetch_blob_body(
        session,
        owner_repo="acme/project",
        commit="commit-sha",
        path="docs/start.md",
        blob_sha="blob-sha",
        api_headers={"User-Agent": "test"},
        user_agent="test",
    )

    assert raw == b"# docs"
    assert url == "https://raw.githubusercontent.com/acme/project/commit-sha/docs/start.md"
    assert transport == "RAW_GITHUB_ANONYMOUS"


def test_rate_limit_detection_honors_retry_after_and_reset():
    response = _Response(status_code=429, headers={"Retry-After": "37"})
    assert _is_rate_limited(response)
    assert _retry_after_seconds(response) == 37

    response = _Response(
        status_code=403,
        headers={
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(int(time.time()) + 20),
        },
    )
    assert _is_rate_limited(response)
    assert 1 <= int(_retry_after_seconds(response) or 0) <= 30


def test_anonymous_429_is_raised_as_circuit_breaker_signal():
    session = _Session(_Response(status_code=429, headers={"Retry-After": "60"}))
    with pytest.raises(GitHubRateLimitError) as caught:
        _fetch_blob_body(
            session,
            owner_repo="acme/project",
            commit="commit-sha",
            path="docs/start.md",
            blob_sha="blob-sha",
            api_headers={"User-Agent": "test"},
            user_agent="test",
        )

    assert caught.value.status_code == 429
    assert caught.value.retry_after_seconds == 60
    assert len(session.calls) == 1
