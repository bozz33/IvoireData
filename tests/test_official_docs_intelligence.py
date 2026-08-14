from __future__ import annotations

from ivoiredata.connectors import official_docs_strategy as strategy
from ivoiredata.connectors import official_git_versions as versions


def test_ref_strategy_is_inferred_without_source_specific_profiles():
    assert versions.infer_ref_strategy("13.x") == "major_branch"
    assert versions.infer_ref_strategy("5.x") == "major_branch"
    assert versions.infer_ref_strategy("13.3") == "minor_branch"
    assert versions.infer_ref_strategy("main") == "fixed_ref"
    assert versions.infer_ref_strategy("v2.4.1") == "release_tag"


def test_current_stable_resolution_uses_metadata_not_source_id(monkeypatch):
    monkeypatch.setattr(
        versions,
        "_latest_stable_release",
        lambda repository, user_agent: {
            "tag": "v8.2.1",
            "published_at": "2026-08-14T00:00:00Z",
            "release_url": "https://github.com/acme/runtime/releases/tag/v8.2.1",
        },
    )
    called = {}

    def fake_git(**kwargs):
        called.update(kwargs)
        return "resource"

    monkeypatch.setattr(versions, "_base_git_resource", fake_git)
    result = versions.resolving_official_git_docs_resource(
        source_id="prog_any_future_framework",
        repository="acme/docs",
        ref="7.x",
        metadata_base={
            "version_policy": "CURRENT_STABLE",
            "version_repository": "acme/runtime",
        },
    )
    assert result == "resource"
    assert called["repository"] == "acme/docs"
    assert called["ref"] == "8.x"
    assert called["metadata_base"]["detected_doc_version"] == "8.2.1"


def test_auto_strategy_discovers_high_confidence_edit_link(monkeypatch):
    class Response:
        url = "https://docs.example.dev/guide/start"
        headers = {"content-type": "text/html; charset=utf-8"}
        content = b"<html></html>"
        text = """
        <html><body>
          <a href="https://github.com/acme/framework/edit/4.x/docs/guide/start.md">Edit this page</a>
        </body></html>
        """

        def raise_for_status(self):
            return None

    import requests

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response())
    found = strategy.discover_official_git_source("https://docs.example.dev/guide/start", "test")
    assert found is not None
    assert found["repository"] == "acme/framework"
    assert found["ref"] == "4.x"
    assert found["include_prefix"] == "docs/guide/"


def test_auto_strategy_rejects_ambiguous_footer_repository(monkeypatch):
    class Response:
        url = "https://docs.example.dev/"
        headers = {"content-type": "text/html"}
        content = b"<html></html>"
        text = '<a href="https://github.com/acme/random">GitHub</a>'

        def raise_for_status(self):
            return None

    import requests

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response())
    assert strategy.discover_official_git_source("https://docs.example.dev/", "test") is None
