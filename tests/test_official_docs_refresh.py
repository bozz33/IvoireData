from __future__ import annotations

import hashlib
from pathlib import Path

from ivoiredata.connectors.official_docs_refresh import _discover
from ivoiredata.upstream_state import UpstreamState


class FakeResponse:
    def __init__(self, status_code: int, *, url: str, content: bytes = b"", headers=None):
        self.status_code = status_code
        self.url = url
        self.content = content
        self.headers = headers or {}
        self.text = content.decode("utf-8", "replace")

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 304:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=262144):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]


class QueueSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        assert self.responses, f"unexpected request {url}"
        return self.responses.pop(0)


def test_cached_sitemap_is_revalidated_and_replayed_only_after_304(tmp_path: Path):
    sitemap_url = "https://docs.example.test/sitemap.xml"
    sitemap = (
        b'<?xml version="1.0"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b'<url><loc>https://docs.example.test/guide/a</loc><lastmod>2026-08-13</lastmod></url>'
        b'</urlset>'
    )
    local = tmp_path / "sitemap.xml"
    local.write_bytes(sitemap)
    digest = hashlib.sha256(sitemap).hexdigest()
    artifact = "discovery:" + hashlib.sha256(sitemap_url.encode()).hexdigest()[:24]

    upstream = UpstreamState(tmp_path / "upstreams.json")
    upstream.mark_downloaded(
        "docs",
        artifact,
        url=sitemap_url,
        signature=digest,
        sha256=digest,
        size_bytes=len(sitemap),
        etag='"catalog-v1"',
        method="DOCS_DISCOVERY_INDEX",
        local_path=str(local),
        extra={"content_type": "application/xml"},
    )

    session = QueueSession([
        FakeResponse(404, url="https://docs.example.test/robots.txt"),
        FakeResponse(304, url=sitemap_url),
    ])
    pages, info = _discover(
        session,
        source_id="docs",
        final_root="https://docs.example.test/guide/",
        explicit=[sitemap_url],
        upstream=upstream,
        snapshot_dir=tmp_path,
        ua="IvoireData/test",
        max_sitemaps=10,
        max_total_bytes=1_000_000,
    )

    assert [page.url for page in pages] == ["https://docs.example.test/guide/a"]
    assert info["methods"] == ["sitemap"]
    assert len(session.calls) == 2
    assert session.calls[1]["url"] == sitemap_url
    assert session.calls[1]["headers"]["If-None-Match"] == '"catalog-v1"'
