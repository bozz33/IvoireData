from __future__ import annotations

import hashlib
from collections import deque
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

from . import official_docs as base


def _target_prefix(final_root: str) -> str:
    """Return the same effective web scope used by the official-docs crawler."""
    path = urlparse(final_root).path or "/"
    if path.endswith("/"):
        return path
    if "/" not in path:
        return "/"
    parent = path.rsplit("/", 1)[0] + "/"
    return parent or "/"


def _within_target_scope(value: str, *, host: str, prefix: str) -> bool:
    parsed = urlparse(base._url(value))
    if (parsed.hostname or "").casefold() != host.casefold():
        return False
    return prefix == "/" or parsed.path.startswith(prefix)


def _scoped_indexes(final_root: str, explicit: Iterable[str]) -> tuple[list[str], str, str, bool]:
    """Prefer indexes that belong to the resolved documentation subtree.

    The legacy connector also probes ``/llms.txt`` and ``/sitemap.xml`` at the domain
    root. That is correct when the documentation itself owns the whole domain, but it
    is pathological on multi-project hosts such as ``commons.apache.org``: a package
    rooted at ``/proper/commons-lang/`` can otherwise inherit discovery for every
    sibling project on the host.

    Explicit discovery URLs are authoritative and never cause implicit origin-level
    discovery. For an origin-root target, the historical root behavior is preserved.
    """
    values = [str(value).strip() for value in explicit if str(value).strip()]
    parsed = urlparse(final_root)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    prefix = _target_prefix(final_root)
    host = (parsed.hostname or "").casefold()
    if values:
        return (
            list(dict.fromkeys(base._url(urljoin(final_root, value)) for value in values)),
            host,
            prefix,
            False,
        )

    local_base = origin if prefix == "/" else origin + prefix.rstrip("/")
    indexes = [local_base + "/llms.txt", local_base + "/sitemap.xml"]
    return list(dict.fromkeys(indexes)), host, prefix, prefix == "/"


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
    """Conditionally revalidate discovery while staying inside the docs subtree.

    This preserves the v0.8.3 refresh semantics (ETag/Last-Modified revalidation on
    every due run) while removing the unbounded cross-project discovery path that was
    observed in production for Apache Commons Lang. The target subtree is enforced
    before sitemap/llms/html entries enter the page set. Nested sitemap indexes are
    accepted only when they are themselves inside the target subtree; if a local
    sitemap delegates to an origin-global index, it is ignored and the existing
    scope-safe crawl fallback can take over.
    """
    indexes, host, prefix, allow_origin_discovery = _scoped_indexes(final_root, explicit)
    queue = deque(indexes)
    parsed = urlparse(final_root)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # robots.txt is domain-global. Consult its Sitemap directives only when the target
    # itself owns the origin root. A nested package-doc target must not inherit sibling
    # projects from a shared host.
    if allow_origin_discovery:
        try:
            robots = session.get(
                origin + "/robots.txt",
                headers={"User-Agent": ua},
                timeout=30,
            )
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

            accepted_found = [
                item
                for item in found
                if _within_target_scope(item.url, host=host, prefix=prefix)
            ]
            accepted_nested = [
                base._url(urljoin(index, value))
                for value in nested
                if _within_target_scope(
                    base._url(urljoin(index, value)), host=host, prefix=prefix
                )
            ]
            if accepted_found or accepted_nested:
                methods.add("sitemap")
                successful.append(index)
                for item in accepted_found:
                    old = pages.get(item.url)
                    if old is None or (item.lastmod and not old.lastmod):
                        pages[item.url] = item
                queue.extend(accepted_nested)
                continue

            found = [
                item
                for item in base._llms(raw, index)
                if _within_target_scope(item.url, host=host, prefix=prefix)
            ]
            if found:
                methods.add("llms_txt")
                successful.append(index)
                for item in found:
                    pages.setdefault(item.url, item)
                continue

            if "html" in ctype or raw.lstrip().startswith(b"<"):
                _, hrefs = base.html_text_and_links(raw)
                accepted_pages = []
                for href in hrefs:
                    page = base._url(urljoin(index, href))
                    if _within_target_scope(page, host=host, prefix=prefix):
                        accepted_pages.append(page)
                if accepted_pages:
                    methods.add("html_index")
                    successful.append(index)
                    for page in accepted_pages:
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
        "scope_prefix": prefix,
        "origin_discovery_enabled": allow_origin_discovery,
    }


# ``official_docs_resource`` resolves _discover through the defining module globals at
# iteration time. Apply this after ``official_docs_refresh`` so refresh semantics remain
# intact and only discovery scope changes.
base._discover = _discover
