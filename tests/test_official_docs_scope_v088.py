from __future__ import annotations

from types import SimpleNamespace

from ivoiredata.connectors import official_docs as base
from ivoiredata.connectors import official_docs_scope as scope


def test_nested_docs_target_uses_only_subtree_indexes():
    indexes, host, prefix, allow_origin = scope._scoped_indexes(
        "https://commons.apache.org/proper/commons-lang/", ()
    )
    assert host == "commons.apache.org"
    assert prefix == "/proper/commons-lang/"
    assert allow_origin is False
    assert indexes == [
        "https://commons.apache.org/proper/commons-lang/llms.txt",
        "https://commons.apache.org/proper/commons-lang/sitemap.xml",
    ]
    assert "https://commons.apache.org/llms.txt" not in indexes
    assert "https://commons.apache.org/sitemap.xml" not in indexes


def test_explicit_discovery_never_enables_implicit_origin_discovery():
    indexes, _, prefix, allow_origin = scope._scoped_indexes(
        "https://docs.example.dev/guide/",
        ("sitemap-docs.xml",),
    )
    assert prefix == "/guide/"
    assert indexes == ["https://docs.example.dev/guide/sitemap-docs.xml"]
    assert allow_origin is False


def test_origin_root_preserves_origin_discovery_mode():
    indexes, host, prefix, allow_origin = scope._scoped_indexes(
        "https://docs.example.dev/", ()
    )
    assert host == "docs.example.dev"
    assert prefix == "/"
    assert allow_origin is True
    assert indexes == [
        "https://docs.example.dev/llms.txt",
        "https://docs.example.dev/sitemap.xml",
    ]


def test_nested_discovery_skips_global_robots_and_filters_sibling_sitemap_pages(monkeypatch):
    requested: list[str] = []

    class Session:
        def get(self, url, **kwargs):
            raise AssertionError(f"nested discovery must not request origin robots: {url}")

    sitemap = b"""<?xml version='1.0' encoding='UTF-8'?>
    <urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
      <url><loc>https://commons.apache.org/proper/commons-lang/userguide.html</loc></url>
      <url><loc>https://commons.apache.org/proper/commons-io/userguide.html</loc></url>
    </urlset>
    """

    def fake_fetch(session, *, url, **kwargs):
        requested.append(url)
        if url.endswith("/llms.txt"):
            raise RuntimeError("no local llms.txt")
        if url.endswith("/sitemap.xml"):
            response = SimpleNamespace(
                url=url,
                headers={"content-type": "application/xml"},
            )
            return response, sitemap, "digest", None, False, False
        raise AssertionError(f"unexpected discovery request: {url}")

    monkeypatch.setattr(base, "_fetch", fake_fetch)
    pages, stats = scope._discover(
        Session(),
        source_id="commons-lang3",
        final_root="https://commons.apache.org/proper/commons-lang/",
        explicit=(),
        upstream=None,
        snapshot_dir=None,
        ua="IvoireData-test",
        max_sitemaps=1000,
        max_total_bytes=10_000_000,
    )

    assert requested == [
        "https://commons.apache.org/proper/commons-lang/llms.txt",
        "https://commons.apache.org/proper/commons-lang/sitemap.xml",
    ]
    assert [item.url for item in pages] == [
        "https://commons.apache.org/proper/commons-lang/userguide.html"
    ]
    assert stats["scope_prefix"] == "/proper/commons-lang/"
    assert stats["origin_discovery_enabled"] is False
    assert stats["methods"] == ["sitemap"]
    assert stats["truncated"] is False


def test_package_import_activates_scoped_discovery_after_refresh_hardening():
    assert base._discover is scope._discover
