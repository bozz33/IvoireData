from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urldefrag, urljoin, urlparse

from ..cleaning import clean_text
from ..metadata import classify_from_base, title_from_text
from ..snapshots import save_snapshot
from ..state_io import atomic_write_json
from ..upstream_state import UpstreamState
from .public_web import _robots_allowed, chunk_text, html_text_and_links

_SKIP_SUFFIXES = {
    ".7z", ".avi", ".bin", ".bmp", ".css", ".dmg", ".doc", ".docx", ".epub",
    ".exe", ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".js", ".map", ".mp3",
    ".mp4", ".pdf", ".png", ".rar", ".svg", ".tar", ".tgz", ".webm", ".webp",
    ".woff", ".woff2", ".zip",
}
_DEFAULT_EXCLUDES = (
    r"/(?:blog|news|community|showcase|partners?|pricing|jobs?|events?)(?:/|$)",
    r"/(?:login|signin|signup|account|search)(?:/|$)",
    r"[?&](?:q|query|search)=",
)


@dataclass(frozen=True)
class DiscoveryEntry:
    url: str
    lastmod: str | None = None


def _canonical_url(url: str) -> str:
    value = urldefrag(url.strip())[0]
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    # Tracking parameters create duplicate pages but are never part of documentation
    # identity. Keep functional query strings because some official references use them.
    return parsed._replace(fragment="").geturl()


def _is_htmlish_url(url: str) -> bool:
    path = urlparse(url).path.lower().rstrip("/")
    suffix = Path(path).suffix.lower()
    return suffix not in _SKIP_SUFFIXES


def _same_host(url: str, host: str) -> bool:
    return (urlparse(url).hostname or "").lower() == host.lower()


def _matches_scope(url: str, *, host: str, include_prefixes: list[str], exclude_patterns: list[re.Pattern[str]]) -> bool:
    if not _same_host(url, host) or not _is_htmlish_url(url):
        return False
    parsed = urlparse(url)
    if include_prefixes and not any(parsed.path.startswith(prefix) for prefix in include_prefixes):
        return False
    target = parsed.path + (("?" + parsed.query) if parsed.query else "")
    return not any(pattern.search(target) for pattern in exclude_patterns)


def _sitemap_payload(raw: bytes) -> tuple[list[DiscoveryEntry], list[str]]:
    """Parse an XML sitemap or sitemap index without depending on namespaces."""
    root = ET.fromstring(raw)
    tag = root.tag.rsplit("}", 1)[-1].lower()
    entries: list[DiscoveryEntry] = []
    indexes: list[str] = []
    if tag == "sitemapindex":
        for item in root:
            if item.tag.rsplit("}", 1)[-1].lower() != "sitemap":
                continue
            loc = None
            for child in item:
                if child.tag.rsplit("}", 1)[-1].lower() == "loc" and child.text:
                    loc = child.text.strip()
                    break
            if loc:
                indexes.append(loc)
        return entries, indexes
    if tag != "urlset":
        return entries, indexes
    for item in root:
        if item.tag.rsplit("}", 1)[-1].lower() != "url":
            continue
        loc = None
        lastmod = None
        for child in item:
            name = child.tag.rsplit("}", 1)[-1].lower()
            if name == "loc" and child.text:
                loc = child.text.strip()
            elif name == "lastmod" and child.text:
                lastmod = child.text.strip()
        if loc:
            entries.append(DiscoveryEntry(_canonical_url(loc), lastmod))
    return entries, indexes


def _llms_links(raw: bytes, base_url: str) -> list[DiscoveryEntry]:
    text = raw.decode("utf-8", "replace")
    links: list[str] = []
    for match in re.finditer(r"\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)", text):
        links.append(match.group(1).strip("<>"))
    for line in text.splitlines():
        stripped = line.strip().strip("<>")
        if stripped.startswith(("http://", "https://")):
            links.append(stripped.split()[0])
    return [DiscoveryEntry(_canonical_url(urljoin(base_url, link))) for link in dict.fromkeys(links)]


def _robots_sitemaps(text: str, base_url: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().casefold() == "sitemap" and value.strip():
            out.append(_canonical_url(urljoin(base_url, value.strip())))
    return list(dict.fromkeys(out))


def _body_cache(upstream: UpstreamState | None, source_id: str, artifact: str):
    if upstream is None:
        return {}, None, None, None
    cached = upstream.get(source_id, artifact)
    path = upstream.cached_path(source_id, artifact)
    if path is None:
        return cached, None, None, None
    try:
        raw = path.read_bytes()
    except OSError:
        return cached, None, None, None
    digest = hashlib.sha256(raw).hexdigest()
    expected = str(cached.get("sha256") or cached.get("signature") or "").strip()
    if expected and expected != digest:
        return cached, None, None, None
    return cached, path, raw, digest


def _request_cached(
    session,
    *,
    url: str,
    source_id: str,
    artifact: str,
    upstream: UpstreamState | None,
    snapshot_dir: Path | None,
    user_agent: str,
    accept: str,
    timeout: int,
    committed_signature: str | None = None,
    method: str,
):
    """Conditional GET that can replay an integrity-checked body after a 304.

    A network validator is trusted directly only when the matching body is already
    committed by dlt. Otherwise an exact local snapshot must exist so an interrupted run
    can finish without downloading the same bytes again. If neither condition holds, a
    304 is retried unconditionally.
    """
    cached, cached_path, cached_raw, cached_digest = _body_cache(upstream, source_id, artifact)
    headers = {"User-Agent": user_agent, "Accept": accept}
    validator_safe = bool(
        upstream
        and (
            (committed_signature and cached.get("signature") == committed_signature)
            or cached_raw is not None
        )
    )
    if validator_safe and upstream:
        headers.update(upstream.conditional_headers(source_id, artifact))

    response = session.get(url, headers=headers, timeout=timeout)
    if response.status_code == 304:
        if committed_signature and cached.get("signature") == committed_signature:
            if upstream:
                upstream.mark_http_unchanged(
                    source_id, artifact, url=response.url,
                    extra={"signature": committed_signature, "local_path": str(cached_path) if cached_path else None},
                )
            return response, None, committed_signature, cached_path, False, True
        if cached_raw is not None and cached_digest is not None and cached_path is not None:
            if upstream:
                upstream.mark_http_unchanged(
                    source_id, artifact, url=response.url,
                    extra={"signature": cached_digest, "local_path": str(cached_path)},
                )
            return response, cached_raw, cached_digest, cached_path, True, True
        response = session.get(
            url,
            headers={"User-Agent": user_agent, "Accept": accept},
            timeout=timeout,
        )

    response.raise_for_status()
    raw = response.content
    digest = hashlib.sha256(raw).hexdigest()
    if committed_signature and committed_signature == digest:
        if upstream:
            upstream.mark_unchanged(
                source_id, artifact, signature=digest, url=response.url, reason="SHA256",
                etag=response.headers.get("etag"), last_modified=response.headers.get("last-modified"),
                extra={"content_type": response.headers.get("content-type")},
            )
        return response, None, digest, cached_path, False, False

    snapshot = save_snapshot(
        snapshot_dir,
        source_id=source_id,
        url=response.url,
        content=raw,
        content_type=response.headers.get("content-type"),
        name=f"{method.lower()}-{hashlib.sha256(url.encode()).hexdigest()[:16]}",
    )
    path_value = snapshot.get("local_path")
    local_path = Path(str(path_value)) if path_value else None
    if upstream:
        upstream.mark_downloaded(
            source_id, artifact, url=response.url, signature=digest,
            sha256=digest, size_bytes=len(raw), etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"), method=method,
            local_path=str(local_path) if local_path else None,
            extra={"content_type": response.headers.get("content-type")},
        )
    return response, raw, digest, local_path, False, False


def _discovery_candidates(final_root: str, explicit: Iterable[str]) -> list[str]:
    if explicit:
        return [_canonical_url(urljoin(final_root, str(value))) for value in explicit if str(value).strip()]
    parsed = urlparse(final_root)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path
    if not path.endswith("/"):
        path = path.rsplit("/", 1)[0] + "/" if "/" in path else "/"
    path_base = origin + path.rstrip("/")
    candidates = [
        path_base + "/llms.txt",
        origin + "/llms.txt",
        path_base + "/sitemap.xml",
        origin + "/sitemap.xml",
    ]
    return list(dict.fromkeys(_canonical_url(value) for value in candidates))


def _discover_from_indexes(
    session,
    *,
    source_id: str,
    final_root: str,
    explicit_urls: Iterable[str],
    upstream: UpstreamState | None,
    snapshot_dir: Path | None,
    user_agent: str,
    max_sitemaps: int,
):
    queue = deque(_discovery_candidates(final_root, explicit_urls))
    parsed_root = urlparse(final_root)
    origin = f"{parsed_root.scheme}://{parsed_root.netloc}"
    robots_url = origin + "/robots.txt"
    try:
        robots = session.get(robots_url, headers={"User-Agent": user_agent}, timeout=30)
        if robots.ok:
            for candidate in _robots_sitemaps(robots.text, final_root):
                queue.append(candidate)
    except Exception:
        pass

    entries: dict[str, DiscoveryEntry] = {}
    visited: set[str] = set()
    successful_indexes: list[str] = []
    methods: set[str] = set()
    while queue and len(visited) < max(1, max_sitemaps):
        index_url = _canonical_url(queue.popleft())
        if index_url in visited:
            continue
        visited.add(index_url)
        artifact = "discovery:" + hashlib.sha256(index_url.encode()).hexdigest()[:24]
        try:
            response, raw, digest, cached_path, replayed, http304 = _request_cached(
                session,
                url=index_url,
                source_id=source_id,
                artifact=artifact,
                upstream=upstream,
                snapshot_dir=snapshot_dir,
                user_agent=user_agent,
                accept="application/xml,text/xml,text/plain,text/markdown,text/html,*/*;q=0.1",
                timeout=120,
                committed_signature=None,
                method="DOCS_DISCOVERY_INDEX",
            )
            if raw is None and cached_path is not None:
                raw = cached_path.read_bytes()
            if raw is None:
                continue
            ctype = (response.headers.get("content-type") or "").lower()
            lower_url = index_url.lower()
            if "xml" in ctype or lower_url.endswith((".xml", ".xml.gz")) or raw.lstrip().startswith(b"<"):
                try:
                    found, nested = _sitemap_payload(raw)
                except ET.ParseError:
                    found, nested = [], []
                if found or nested:
                    methods.add("sitemap")
                    successful_indexes.append(index_url)
                    for entry in found:
                        previous = entries.get(entry.url)
                        if previous is None or (entry.lastmod and not previous.lastmod):
                            entries[entry.url] = entry
                    for nested_url in nested:
                        queue.append(_canonical_url(urljoin(index_url, nested_url)))
                    continue
            text = raw.decode("utf-8", "replace")
            llms = _llms_links(raw, index_url)
            if llms:
                methods.add("llms_txt")
                successful_indexes.append(index_url)
                for entry in llms:
                    entries.setdefault(entry.url, entry)
                continue
            # Explicit HTML indexes are useful for docs sites that publish a complete TOC
            # but no sitemap/llms.txt.
            if "html" in ctype or raw.lstrip().startswith(b"<"):
                _, hrefs = html_text_and_links(raw)
                if hrefs:
                    methods.add("html_index")
                    successful_indexes.append(index_url)
                    for href in hrefs:
                        page = _canonical_url(urljoin(index_url, href))
                        entries.setdefault(page, DiscoveryEntry(page))
        except Exception:
            continue

    return list(entries.values()), {
        "methods": sorted(methods),
        "indexes_checked": len(visited),
        "successful_indexes": successful_indexes,
    }


def _crawl_discovery(
    session,
    *,
    final_root: str,
    host: str,
    include_prefixes: list[str],
    exclude_patterns: list[re.Pattern[str]],
    user_agent: str,
    max_pages: int,
):
    """Bounded fallback when an official documentation index is unavailable.

    The crawler is deliberately reported as best-effort; a cap hit means completeness is
    not proven. Sitemap/llms discovery is preferred and is the normal production path.
    """
    queue = deque([final_root])
    seen: set[str] = set()
    found: list[DiscoveryEntry] = []
    robots_cache: dict[str, Any] = {}
    while queue and len(seen) < max_pages:
        current = _canonical_url(queue.popleft())
        if current in seen or not _matches_scope(
            current, host=host, include_prefixes=include_prefixes, exclude_patterns=exclude_patterns
        ):
            continue
        seen.add(current)
        found.append(DiscoveryEntry(current))
        if not _robots_allowed(session, current, user_agent, robots_cache):
            continue
        try:
            response = session.get(current, headers={"User-Agent": user_agent}, timeout=90)
            response.raise_for_status()
        except Exception:
            continue
        ctype = (response.headers.get("content-type") or "").lower()
        if "html" not in ctype and not response.content.lstrip().startswith(b"<"):
            continue
        _, hrefs = html_text_and_links(response.content)
        for href in hrefs:
            candidate = _canonical_url(urljoin(response.url, href))
            if candidate not in seen and _matches_scope(
                candidate, host=host, include_prefixes=include_prefixes, exclude_patterns=exclude_patterns
            ):
                queue.append(candidate)
    return found, bool(queue)


def _extract_document(raw: bytes, content_type: str, url: str) -> str:
    ctype = (content_type or "").lower()
    if "html" in ctype or raw.lstrip().startswith(b"<"):
        return html_text_and_links(raw)[0]
    if any(token in ctype for token in ("markdown", "text/plain", "application/json", "text/")):
        return clean_text(raw.decode("utf-8", "replace"))
    # Some official docs serve Markdown without a useful Content-Type.
    if url.lower().endswith((".md", ".txt")):
        return clean_text(raw.decode("utf-8", "replace"))
    return ""


def official_docs_resource(
    *,
    source_id: str,
    url: str,
    user_agent: str = "IvoireData/0.8.3",
    discovery_urls: Iterable[str] = (),
    include_prefixes: Iterable[str] = (),
    exclude_patterns: Iterable[str] = (),
    max_pages: int = 20_000,
    max_sitemaps: int = 200,
    max_bytes_per_page: int = 8_000_000,
    max_new_bytes_per_run: int = 500_000_000,
    request_pause_seconds: float = 0.02,
    allow_crawl_fallback: bool = True,
    snapshot_dir: Path | None = None,
    metadata_base: dict[str, Any] | None = None,
    upstream_state_path: Path | None = None,
    license_name: str | None = None,
    license_url: str | None = None,
    training_eligible: bool = False,
    license_review_status: str = "UNREVIEWED",
):
    """Synchronize a complete official developer-documentation corpus incrementally.

    Discovery priority is official llms.txt / sitemap / sitemap-index / robots-declared
    sitemap, then an explicitly visible HTML index. A bounded same-host crawl is only a
    fallback and is reported as best-effort. Per-page sitemap `lastmod` can skip network
    requests entirely; otherwise ETag/Last-Modified and SHA-256 prevent duplicate bodies
    and rows. Crash recovery replays exact local snapshots after a 304 when dlt did not
    commit the preceding run.

    Downloading and training eligibility are intentionally separate. `training_eligible`
    is metadata for downstream corpus builders and must only be enabled after license
    review; official/public does not automatically mean redistributable or trainable.
    """
    import dlt
    import requests

    max_pages = max(1, int(max_pages))
    max_sitemaps = max(1, int(max_sitemaps))
    max_bytes_per_page = max(100_000, int(max_bytes_per_page))
    max_new_bytes_per_run = max(0, int(max_new_bytes_per_run))
    pause = max(0.0, min(float(request_pause_seconds), 2.0))
    base = dict(metadata_base or {})
    configured_prefixes = [str(value) for value in include_prefixes if str(value)]
    compiled_excludes = [re.compile(value, re.I) for value in (*_DEFAULT_EXCLUDES, *tuple(exclude_patterns))]

    @dlt.resource(name="official_docs", write_disposition="merge", primary_key="record_id")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent})
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None
        dlt_state = dlt.current.resource_state()
        committed_hashes = dlt_state.setdefault("content_hashes_v1", {})
        committed_lastmods = dlt_state.setdefault("sitemap_lastmods_v1", {})
        previous_known = set(str(value) for value in dlt_state.get("known_urls_v1", []) if value)

        root_response = session.get(url, headers={"User-Agent": user_agent}, timeout=120)
        root_response.raise_for_status()
        final_root = _canonical_url(root_response.url)
        host = (urlparse(final_root).hostname or "").lower()
        if not host:
            raise RuntimeError(f"official docs root has no host: {final_root}")

        prefixes = list(configured_prefixes)
        if not prefixes:
            path = urlparse(final_root).path or "/"
            if not path.endswith("/"):
                path = path.rsplit("/", 1)[0] + "/" if "/" in path else "/"
            prefixes = [path]

        entries, discovery = _discover_from_indexes(
            session,
            source_id=source_id,
            final_root=final_root,
            explicit_urls=discovery_urls,
            upstream=upstream,
            snapshot_dir=snapshot_dir,
            user_agent=user_agent,
            max_sitemaps=max_sitemaps,
        )
        crawl_truncated = False
        discovery_fallback = False
        if not entries and allow_crawl_fallback:
            entries, crawl_truncated = _crawl_discovery(
                session,
                final_root=final_root,
                host=host,
                include_prefixes=prefixes,
                exclude_patterns=compiled_excludes,
                user_agent=user_agent,
                max_pages=max_pages,
            )
            discovery_fallback = True
            discovery["methods"] = ["crawl_fallback"]

        scoped: dict[str, DiscoveryEntry] = {}
        for entry in entries:
            page_url = _canonical_url(entry.url)
            if _matches_scope(
                page_url,
                host=host,
                include_prefixes=prefixes,
                exclude_patterns=compiled_excludes,
            ):
                prior = scoped.get(page_url)
                if prior is None or (entry.lastmod and not prior.lastmod):
                    scoped[page_url] = DiscoveryEntry(page_url, entry.lastmod)

        ordered = sorted(scoped.values(), key=lambda item: item.url)
        discovery_truncated = crawl_truncated or len(ordered) > max_pages
        if len(ordered) > max_pages:
            ordered = ordered[:max_pages]

        stats: dict[str, Any] = {
            "source_id": source_id,
            "root_url": url,
            "final_root_url": final_root,
            "discovery_methods": discovery.get("methods", []),
            "discovery_indexes_checked": discovery.get("indexes_checked", 0),
            "successful_discovery_indexes": discovery.get("successful_indexes", []),
            "discovery_fallback": discovery_fallback,
            "discovery_complete": not discovery_truncated and bool(ordered),
            "discovery_truncated": discovery_truncated,
            "discovered_pages": len(scoped),
            "selected_pages": len(ordered),
            "unchanged_lastmod": 0,
            "unchanged_http304": 0,
            "unchanged_sha256": 0,
            "downloaded": 0,
            "replayed_from_local_cache": 0,
            "failed": 0,
            "skipped_oversize": 0,
            "deferred_budget": 0,
            "downloaded_bytes": 0,
            "business_chunks": 0,
            "removed_upstream": 0,
            "backlog_count": 0,
            "failures": [],
            "license_name": license_name,
            "license_url": license_url,
            "license_review_status": license_review_status,
            "training_eligible": bool(training_eligible),
        }

        if not ordered:
            raise RuntimeError(
                f"no official documentation pages discovered for {source_id}; "
                "configure discovery_urls/include_prefixes instead of silently accepting an incomplete corpus"
            )

        current_urls = {entry.url for entry in ordered}
        if not discovery_truncated:
            removed = sorted(previous_known - current_urls)
            stats["removed_upstream"] = len(removed)
            for page_url in removed:
                record_id = hashlib.sha256(f"{source_id}|page|{page_url}".encode()).hexdigest()
                yield dlt.mark.with_table_name({
                    "record_id": record_id,
                    "source_id": source_id,
                    "page_url": page_url,
                    "active": False,
                    "content_sha256": committed_hashes.get(page_url),
                    "lastmod": committed_lastmods.get(page_url),
                    "license_name": license_name,
                    "license_url": license_url,
                    "license_review_status": license_review_status,
                    "training_eligible": bool(training_eligible),
                }, "official_docs_pages")
                if upstream:
                    upstream.mark_removed(source_id, f"page:{page_url}")

        budget_remaining = max_new_bytes_per_run
        robots_cache: dict[str, Any] = {}
        for entry in ordered:
            page_url = entry.url
            committed_digest = committed_hashes.get(page_url)
            prior_lastmod = committed_lastmods.get(page_url)

            if committed_digest and entry.lastmod and prior_lastmod == entry.lastmod:
                stats["unchanged_lastmod"] += 1
                if upstream:
                    upstream.mark_unchanged(
                        source_id, f"page:{page_url}", signature=committed_digest,
                        url=page_url, reason="SITEMAP_LASTMOD",
                        extra={"lastmod": entry.lastmod},
                    )
                continue

            if not _robots_allowed(session, page_url, user_agent, robots_cache):
                stats["failed"] += 1
                stats["failures"].append({"url": page_url, "error": "ROBOTS_DISALLOWED"})
                continue

            artifact = f"page:{page_url}"
            try:
                cached = upstream.get(source_id, artifact) if upstream else {}
                cached_size = int(cached.get("size_bytes") or 0)
                if not committed_digest and cached_size and cached_size > budget_remaining and budget_remaining > 0:
                    # Cached bytes do not consume network budget; they can still be replayed.
                    pass
                response, raw, digest, local_path, replayed, http304 = _request_cached(
                    session,
                    url=page_url,
                    source_id=source_id,
                    artifact=artifact,
                    upstream=upstream,
                    snapshot_dir=snapshot_dir,
                    user_agent=user_agent,
                    accept="text/markdown,text/html,application/xhtml+xml,text/plain,*/*;q=0.1",
                    timeout=180,
                    committed_signature=committed_digest,
                    method="OFFICIAL_DOC_PAGE",
                )

                if raw is None:
                    if http304:
                        stats["unchanged_http304"] += 1
                    else:
                        stats["unchanged_sha256"] += 1
                    if entry.lastmod:
                        committed_lastmods[page_url] = entry.lastmod
                    continue

                if len(raw) > max_bytes_per_page:
                    stats["skipped_oversize"] += 1
                    stats["failures"].append({
                        "url": page_url, "error": "PAGE_TOO_LARGE", "size_bytes": len(raw),
                    })
                    continue

                from_network = not replayed
                if from_network and len(raw) > budget_remaining:
                    stats["deferred_budget"] += 1
                    # The body is already content-addressed locally by _request_cached;
                    # next run will replay it without paying the network cost again.
                    continue

                if from_network:
                    budget_remaining = max(0, budget_remaining - len(raw))
                    stats["downloaded"] += 1
                    stats["downloaded_bytes"] += len(raw)
                else:
                    stats["replayed_from_local_cache"] += 1

                content_type = str(response.headers.get("content-type") or cached.get("content_type") or "")
                text = _extract_document(raw, content_type, page_url)
                if not text:
                    stats["failed"] += 1
                    stats["failures"].append({"url": page_url, "error": "NO_EXTRACTABLE_TEXT"})
                    continue

                classified = classify_from_base(base, page_url, text, document_type="DEVELOPER_DOCUMENTATION")
                document_title = title_from_text(text)
                page_record_id = hashlib.sha256(f"{source_id}|page|{page_url}".encode()).hexdigest()
                yield dlt.mark.with_table_name({
                    "record_id": page_record_id,
                    "source_id": source_id,
                    "page_url": page_url,
                    "document_title": document_title,
                    "active": True,
                    "content_sha256": digest,
                    "lastmod": entry.lastmod,
                    "content_type": content_type or None,
                    "local_snapshot": str(local_path) if local_path else None,
                    "license_name": license_name,
                    "license_url": license_url,
                    "license_review_status": license_review_status,
                    "training_eligible": bool(training_eligible),
                    **classified,
                }, "official_docs_pages")

                emitted = 0
                for index, chunk in enumerate(chunk_text(text, size=5000, overlap=300)):
                    chunk_id = hashlib.sha256(
                        f"{source_id}|{page_url}|{digest}|{index}".encode()
                    ).hexdigest()
                    emitted += 1
                    yield dlt.mark.with_table_name({
                        "record_id": chunk_id,
                        "chunk_id": chunk_id,
                        "source_id": source_id,
                        "source_url": page_url,
                        "page_url": page_url,
                        "document_title": document_title,
                        "content_sha256": digest,
                        "chunk_index": index,
                        "content_type": content_type or None,
                        "local_snapshot": str(local_path) if local_path else None,
                        "text": chunk,
                        "active_at_ingest": True,
                        "license_name": license_name,
                        "license_url": license_url,
                        "license_review_status": license_review_status,
                        "training_eligible": bool(training_eligible),
                        **classified,
                    }, "official_docs_chunks")
                stats["business_chunks"] += emitted
                committed_hashes[page_url] = digest
                if entry.lastmod:
                    committed_lastmods[page_url] = entry.lastmod
                if pause:
                    time.sleep(pause)
            except Exception as exc:
                stats["failed"] += 1
                status = getattr(getattr(exc, "response", None), "status_code", None)
                stats["failures"].append({"url": page_url, "status": status, "error": str(exc)[:1000]})
                if upstream:
                    upstream.mark_error(
                        source_id, artifact, url=page_url, error=str(exc),
                        status_code=status, method="OFFICIAL_DOC_PAGE",
                    )

        dlt_state["known_urls_v1"] = sorted(current_urls)
        stats["backlog_count"] = stats["deferred_budget"] + stats["skipped_oversize"] + stats["failed"]
        if snapshot_dir:
            atomic_write_json(snapshot_dir / "official_docs_sync_stats.json", stats)
        stats_record = hashlib.sha256(f"{source_id}|official-docs-stats".encode()).hexdigest()
        yield dlt.mark.with_table_name({
            "record_id": stats_record,
            "run_stats_json": json.dumps(stats, ensure_ascii=False),
            **stats,
        }, "official_docs_sync_stats")

    return resource()
