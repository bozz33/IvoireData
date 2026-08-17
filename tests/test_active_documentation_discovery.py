from __future__ import annotations

import json
from pathlib import Path

import requests

from ivoiredata.technology_authority import OfficialAuthorityResolver as LegacyAuthorityResolver
from ivoiredata.technology_documentation import DocumentationTargetResolver
from ivoiredata.technology_documentation_discovery_runtime import ActiveDocumentationDiscovery
from ivoiredata.technology_harvester import HarvestCandidate, TechnologyHarvestQueue
from ivoiredata.technology_qualification import TechnologyQualificationEngine as LegacyQualificationEngine


class FakeResponse:
    def __init__(
        self,
        url: str,
        *,
        status: int = 200,
        payload=None,
        body: bytes | None = None,
        content_type: str = "application/json",
    ):
        self.url = url
        self.status_code = status
        self._payload = payload
        self.content = body if body is not None else json.dumps(payload if payload is not None else {}).encode()
        self.headers = {
            "content-type": content_type,
            "content-length": str(len(self.content)),
        }

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.content.decode())

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} error for {self.url}")
            error.response = self
            raise error


class FakeSession:
    def __init__(self, routes: dict[str, FakeResponse]):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str, **kwargs):
        self.calls.append(url)
        response = self.routes.get(url)
        if response is None:
            return FakeResponse(url, status=404, payload={})
        return response


def _legacy_verified_central_target(
    queue: TechnologyHarvestQueue,
    *,
    name: str = "com.example:project",
    repository: str = "https://github.com/example/project",
    website: str = "https://central.sonatype.com/artifact/com.example/project/1.0.0",
) -> str:
    central = "https://central.sonatype.com/artifact/com.example/project/1.0.0"
    queue.upsert_many([
        HarvestCandidate("repo1.maven.org", name, "maven-full-index", 85),
    ])
    legacy = LegacyQualificationEngine(
        queue=queue,
        user_agent="test",
        native_resolver=lambda registry, package: {
            "authority_source": "maven",
            "native_registry_url": "https://repo1.maven.org/maven2/example/maven-metadata.xml",
            "name": package,
            "latest_stable_version": "1.0.0",
            "canonical_repository": repository,
            "documentation_url": central,
            "official_website": website,
            "downloads_total": 1_000_000,
            "dependents_count": 10_000,
        },
    )
    assert legacy.run(limit=1, registry="maven")["ready_for_authority"] == 1
    authority = LegacyAuthorityResolver(
        queue=queue,
        user_agent="test",
        crosscheck_resolver=lambda row, native: {
            "ecosystems": {"repository_url": repository},
            "deps_package": {},
            "deps_version": {},
            "deps_links": {"SOURCE_REPO": repository},
            "sources": ["ecosyste.ms", "deps.dev"],
            "errors": [],
        },
    )
    assert authority.run(limit=1, registry="maven")["verified"] == 1
    targets = DocumentationTargetResolver(queue=queue)
    assert targets.run(limit=1, registry="maven")["ready_for_docs_connector"] == 1
    row = queue.db.execute(
        "SELECT target_url FROM documentation_targets WHERE registry='repo1.maven.org' AND name=?",
        (name,),
    ).fetchone()
    assert row["target_url"] == central
    return central


def _public_dns(host: str) -> list[str]:
    return ["93.184.216.34"]


def test_legacy_maven_central_target_is_replaced_by_verified_repo_docs_directory(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        central = _legacy_verified_central_target(queue)
        routes = {
            "https://api.github.com/repos/example/project": FakeResponse(
                "https://api.github.com/repos/example/project",
                payload={
                    "html_url": "https://github.com/example/project",
                    "default_branch": "main",
                    "homepage": "",
                },
            ),
            "https://api.github.com/repos/example/project/contents": FakeResponse(
                "https://api.github.com/repos/example/project/contents",
                payload=[
                    {
                        "type": "dir",
                        "name": "docs",
                        "path": "docs",
                        "html_url": "https://github.com/example/project/tree/main/docs",
                    }
                ],
            ),
            "https://api.github.com/repos/example/project/readme": FakeResponse(
                "https://api.github.com/repos/example/project/readme",
                status=404,
                payload={},
            ),
        }
        session = FakeSession(routes)
        discovery = ActiveDocumentationDiscovery(
            queue=queue,
            user_agent="test",
            session=session,
            host_resolver=_public_dns,
        )
        result = discovery.run(limit=1, registry="maven")
        assert result["discovered"] == 1
        assert result["outcomes"][0]["selected_kind"] == "CANONICAL_REPOSITORY_DOCS_DIRECTORY"
        assert result["outcomes"][0]["selected_url"] == "https://github.com/example/project/tree/main/docs"
        assert central not in session.calls

        target = queue.db.execute(
            "SELECT target_url,target_kind,target_status,target_confidence,evidence_json FROM documentation_targets"
        ).fetchone()
        assert target["target_url"] == "https://github.com/example/project/tree/main/docs"
        assert target["target_status"] == "READY_FOR_DOCS_CONNECTOR"
        assert target["target_confidence"] == "ACTIVE_DISCOVERY_VERIFIED"
        assert "REGISTRY_LANDING_REJECTED_AS_DOCUMENTATION" in target["evidence_json"]
        assert "VERIFIED_REPOSITORY_DOCS_DIRECTORY" in target["evidence_json"]

        # The discovery result checkpoints the *new* target generation, so it is
        # idempotent and does not immediately repeat the same GitHub requests.
        second = discovery.run(limit=1, registry="maven")
        assert second["selected"] == 0
    finally:
        queue.close()


def test_official_site_documentation_link_beats_generic_repository_homepage(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _legacy_verified_central_target(
            queue,
            repository="https://github.com/example/project",
            website="https://project.example/",
        )
        website = b'<html><body><a href="/docs/">Documentation</a></body></html>'
        routes = {
            "https://api.github.com/repos/example/project": FakeResponse(
                "https://api.github.com/repos/example/project",
                payload={
                    "html_url": "https://github.com/example/project",
                    "default_branch": "main",
                    "homepage": "https://project.example/",
                },
            ),
            "https://api.github.com/repos/example/project/contents": FakeResponse(
                "https://api.github.com/repos/example/project/contents",
                payload=[],
            ),
            "https://api.github.com/repos/example/project/readme": FakeResponse(
                "https://api.github.com/repos/example/project/readme",
                status=404,
                payload={},
            ),
            "https://project.example/": FakeResponse(
                "https://project.example/",
                body=website,
                content_type="text/html",
            ),
            "https://project.example/llms.txt": FakeResponse(
                "https://project.example/llms.txt",
                status=404,
                body=b"",
                content_type="text/plain",
            ),
            "https://project.example/docs": FakeResponse(
                "https://project.example/docs",
                body=b"<html>docs</html>",
                content_type="text/html",
            ),
        }
        discovery = ActiveDocumentationDiscovery(
            queue=queue,
            user_agent="test",
            session=FakeSession(routes),
            host_resolver=_public_dns,
        )
        result = discovery.run(limit=1, registry="maven")
        assert result["discovered"] == 1
        assert result["outcomes"][0]["selected_url"] == "https://project.example/docs"
        assert result["outcomes"][0]["selected_kind"] == "OFFICIAL_WEBSITE_DOCS_LINK"
    finally:
        queue.close()


def test_verified_site_llms_txt_can_publish_a_high_confidence_docs_root(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _legacy_verified_central_target(
            queue,
            repository="https://github.com/example/project",
            website="https://project.example/",
        )
        routes = {
            "https://api.github.com/repos/example/project": FakeResponse(
                "https://api.github.com/repos/example/project",
                payload={
                    "html_url": "https://github.com/example/project",
                    "default_branch": "main",
                    "homepage": "https://project.example/",
                },
            ),
            "https://api.github.com/repos/example/project/contents": FakeResponse(
                "https://api.github.com/repos/example/project/contents",
                payload=[],
            ),
            "https://api.github.com/repos/example/project/readme": FakeResponse(
                "https://api.github.com/repos/example/project/readme",
                status=404,
                payload={},
            ),
            "https://project.example/": FakeResponse(
                "https://project.example/",
                body=b"<html>Project</html>",
                content_type="text/html",
            ),
            "https://project.example/llms.txt": FakeResponse(
                "https://project.example/llms.txt",
                body=b"[API Documentation](https://docs.project.example/reference/)",
                content_type="text/plain",
            ),
            "https://docs.project.example/reference": FakeResponse(
                "https://docs.project.example/reference",
                body=b"reference docs",
                content_type="text/html",
            ),
        }
        discovery = ActiveDocumentationDiscovery(
            queue=queue,
            user_agent="test",
            session=FakeSession(routes),
            host_resolver=_public_dns,
        )
        result = discovery.run(limit=1, registry="maven")
        assert result["outcomes"][0]["selected_kind"] == "OFFICIAL_WEBSITE_LLMS_DOCS_LINK"
        assert result["outcomes"][0]["selected_url"] == "https://docs.project.example/reference"
        assert int(result["outcomes"][0]["score"]) == 98
    finally:
        queue.close()


def test_private_official_website_is_rejected_before_any_request(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        _legacy_verified_central_target(
            queue,
            repository="https://gitlab.example.invalid/project/repo",
            website="http://127.0.0.1:8080/docs",
        )
        session = FakeSession({})
        discovery = ActiveDocumentationDiscovery(
            queue=queue,
            user_agent="test",
            session=session,
            host_resolver=_public_dns,
            retry_base_seconds=3600,
        )
        result = discovery.run(limit=1, registry="maven")
        assert result["retry"] == 1
        assert session.calls == []
        target = queue.db.execute(
            "SELECT target_status,target_kind,target_confidence FROM documentation_targets"
        ).fetchone()
        assert target["target_status"] == "DOCS_DISCOVERY_REQUIRED"
        assert target["target_kind"] == "REGISTRY_LANDING_REJECTED"
        assert target["target_confidence"] == "REJECTED_REGISTRY_LANDING"
    finally:
        queue.close()


def test_unlimited_active_discovery_is_rejected(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "harvest.sqlite3")
    try:
        # Initialize all upstream tables needed by the discovery query.
        _legacy_verified_central_target(queue)
        discovery = ActiveDocumentationDiscovery(
            queue=queue,
            user_agent="test",
            session=FakeSession({}),
            host_resolver=_public_dns,
        )
        try:
            discovery.run(limit=0)
        except ValueError as exc:
            assert "intentionally bounded" in str(exc)
        else:
            raise AssertionError("limit=0 must be rejected")
    finally:
        queue.close()
