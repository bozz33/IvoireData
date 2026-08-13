from __future__ import annotations

import hashlib
from collections import deque
from typing import Any
from urllib.parse import urljoin, urlparse

from . import official_docs as base


def _discover(
    session,
    *,
    source_id: str,
    final_root: str,
    explicit,
    upstream,
    snapshot_dir,
    ua: str,
    max_sitemaps: int,
    max_total_bytes: int,
):
    """Revalidate every discovery index conditionally on each due run.

    A cached sitemap/index is never treated as a permanent substitute for the official
    endpoint. The existing ETag/Last-Modified are sent; a 304 then replays the cached body
    locally at zero body-transfer cost. This is required to discover newly added pages.
    """
    queue = deque(base._indexes(final_root, explicit))
    parsed = urlparse(final_root)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    try:
        robots = session.get(origin + "/robots.txt", headers={"User-Agent": ua}, timeout=30)
        if robots.ok:
            for line in robots.text.splitlines():
                key, sep, value = line.partition(":")
                if sep and key.strip().casefold() == "sitemap" and value.strip():
                    queue.append(base._url(value.strip()))
    except Exception:
        pass

    pages: dict[str, Any] = {}
    seen: set[str] = set()
    methods: set[str] = set()
    successful: list[str] = []
    remaining = max(0, int(max_total_bytes))
    downloaded_bytes = 0
    budget_truncated = False

    while queue and len(seen) < max_sitemaps:
        index = base._url(queue.popleft())
        if index in seen:
            continue
        artifact = "discovery:" + hashlib.sha256(index.encode()).hexdigest()[:24]
        _, cached_path, cached_raw, _ = base._cached(upstream, source_id, artifact)
        has_cache = cached_raw is not None and cached_path is not None
        if not has_cache and remaining <= 0:
            budget_truncated = True
            break
        seen.add(index)
        cap = min(50_000_000, max(1, remaining)) if remaining > 0 else 1
        try:
            response, raw, _, path, replayed, _ = base._fetch(
                session,
                url=index,
                source_id=source_id,
                artifact=artifact,
                upstream=upstream,
                snapshot_dir=snapshot_dir,
                user_agent=ua,
                accept="application/xml,text/xml,text/plain,text/markdown,text/html,*/*;q=0.1",
                method="DOCS_DISCOVERY_INDEX",
                cap=cap,
                replay=False,
            )
            if raw is None and path is not None:
                raw = path.read_bytes()
            if raw is None:
                continue
            if not replayed:
                downloaded_bytes += len(raw)
                remaining = max(0, remaining - len(raw))

            ctype = (response.headers.get("content-type") or "").lower()
            try:
                found, nested = base._sitemap(raw)
            except Exception:
                found, nested = [], []
            if found or nested:
                methods.add("sitemap")
                successful.append(index)
                for item in found:
                    old = pages.get(item.url)
                    if old is None or (item.lastmod and not old.lastmod):
                        pages[item.url] = item
                queue.extend(base._url(urljoin(index, value)) for value in nested)
                continue

            found = base._llms(raw, index)
            if found:
                methods.add("llms_txt")
                successful.append(index)
                for item in found:
                    pages.setdefault(item.url, item)
                continue

            if "html" in ctype or raw.lstrip().startswith(b"<"):
                _, hrefs = base.html_text_and_links(raw)
                if hrefs:
                    methods.add("html_index")
                    successful.append(index)
                    for href in hrefs:
                        page = base._url(urljoin(index, href))
                        pages.setdefault(page, base.Entry(page))
        except base.LimitExceeded:
            budget_truncated = True
            break
        except Exception:
            continue

    truncated = budget_truncated or bool(queue and len(seen) >= max_sitemaps)
    return list(pages.values()), {
        "methods": sorted(methods),
        "indexes_checked": len(seen),
        "successful_indexes": successful,
        "truncated": truncated,
        "downloaded_bytes": downloaded_bytes,
        "remaining_bytes": remaining,
    }


def _crawl(
    session,
    *,
    source_id: str,
    root: str,
    host: str,
    prefixes,
    excludes,
    ua: str,
    max_pages: int,
    max_page_bytes: int,
    max_total_bytes: int,
    upstream,
    snapshot_dir,
):
    """Conditionally revalidate cached crawl pages so newly added links are visible."""
    queue = deque([root])
    seen: set[str] = set()
    found: list[Any] = []
    robots_cache: dict[str, Any] = {}
    remaining = max(0, int(max_total_bytes))
    downloaded_bytes = 0
    budget_truncated = False

    while queue and len(seen) < max_pages:
        current = base._url(queue.popleft())
        if current in seen or not base._scope(current, host, prefixes, excludes):
            continue
        artifact = f"page:{current}"
        _, cached_path, cached_raw, _ = base._cached(upstream, source_id, artifact)
        has_cache = cached_raw is not None and cached_path is not None
        if not has_cache and remaining <= 0:
            budget_truncated = True
            break
        seen.add(current)
        if not base._robots_allowed(session, current, ua, robots_cache):
            continue
        cap = min(max_page_bytes, max(1, remaining)) if remaining > 0 else 1
        try:
            response, raw, _, path, replayed, _ = base._fetch(
                session,
                url=current,
                source_id=source_id,
                artifact=artifact,
                upstream=upstream,
                snapshot_dir=snapshot_dir,
                user_agent=ua,
                accept="text/html,application/xhtml+xml,text/markdown,text/plain,*/*;q=0.1",
                method="DOCS_CRAWL_DISCOVERY",
                cap=cap,
                replay=False,
            )
            if raw is None and path is not None:
                raw = path.read_bytes()
            if raw is None:
                continue
            if not replayed:
                downloaded_bytes += len(raw)
                remaining = max(0, remaining - len(raw))
            found.append(base.Entry(current))
            ctype = (response.headers.get("content-type") or "").lower()
            if "html" not in ctype and not raw.lstrip().startswith(b"<"):
                continue
            _, hrefs = base.html_text_and_links(raw)
            for href in hrefs:
                candidate = base._url(urljoin(response.url, href))
                if candidate not in seen and base._scope(candidate, host, prefixes, excludes):
                    queue.append(candidate)
        except base.LimitExceeded:
            budget_truncated = True
            break
        except Exception:
            continue

    return found, budget_truncated or bool(queue), downloaded_bytes, remaining


# official_docs_resource resolves these names through its defining module globals at run
# time. Patch them once during package import without duplicating the large connector.
base._discover = _discover
base._crawl = _crawl
official_docs_resource = base.official_docs_resource
