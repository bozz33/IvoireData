from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlparse

from ..cleaning import clean_text
from ..metadata import classify_from_base, title_from_text
from ..snapshots import save_snapshot
from ..state_io import atomic_write_json
from ..upstream_state import UpstreamState
from .public_web import _robots_allowed, chunk_text, html_text_and_links

_SKIP = {".7z", ".avi", ".bin", ".bmp", ".css", ".dmg", ".doc", ".docx", ".epub", ".exe", ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".js", ".map", ".mp3", ".mp4", ".png", ".rar", ".svg", ".tar", ".tgz", ".webm", ".webp", ".woff", ".woff2", ".zip"}
_EXCLUDES = (r"/(?:blog|news|community|showcase|partners?|pricing|jobs?|events?)(?:/|$)", r"/(?:login|signin|signup|account|search)(?:/|$)", r"[?&](?:q|query|search)=")
_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id", "gclid", "fbclid"}


@dataclass(frozen=True)
class Entry:
    url: str
    lastmod: str | None = None


class LimitExceeded(RuntimeError):
    def __init__(self, url: str, limit: int, declared: int | None = None):
        self.url, self.limit, self.declared = url, int(limit), declared
        super().__init__(f"transfer limit exceeded: {url} limit={limit} declared={declared}")


def _url(value: str) -> str:
    value = urldefrag(value.strip())[0]
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.casefold() not in _TRACKING]
    return parsed._replace(fragment="", query=urlencode(query, doseq=True)).geturl()


def _scope(value: str, host: str, prefixes: list[str], excludes: list[re.Pattern[str]]) -> bool:
    parsed = urlparse(value)
    if (parsed.hostname or "").casefold() != host.casefold() or Path(parsed.path.rstrip("/")).suffix.lower() in _SKIP:
        return False
    if prefixes and not any(parsed.path.startswith(prefix) for prefix in prefixes):
        return False
    target = parsed.path + (("?" + parsed.query) if parsed.query else "")
    return not any(pattern.search(target) for pattern in excludes)


def _sitemap(raw: bytes) -> tuple[list[Entry], list[str]]:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    root = ET.fromstring(raw)
    kind = root.tag.rsplit("}", 1)[-1].lower()
    pages: list[Entry] = []
    nested: list[str] = []
    if kind == "sitemapindex":
        for node in root:
            loc = next((c.text.strip() for c in node if c.tag.rsplit("}", 1)[-1].lower() == "loc" and c.text), None)
            if loc:
                nested.append(_url(loc))
    elif kind == "urlset":
        for node in root:
            loc = next((c.text.strip() for c in node if c.tag.rsplit("}", 1)[-1].lower() == "loc" and c.text), None)
            lastmod = next((c.text.strip() for c in node if c.tag.rsplit("}", 1)[-1].lower() == "lastmod" and c.text), None)
            if loc:
                pages.append(Entry(_url(loc), lastmod))
    return pages, nested


def _llms(raw: bytes, base: str) -> list[Entry]:
    text = raw.decode("utf-8", "replace")
    links = [m.group(1).strip("<>") for m in re.finditer(r"\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)", text)]
    links += [line.strip().strip("<>").split()[0] for line in text.splitlines() if line.strip().startswith(("http://", "https://"))]
    return [Entry(_url(urljoin(base, link))) for link in dict.fromkeys(links)]


def _cached(upstream: UpstreamState | None, source_id: str, artifact: str):
    if upstream is None:
        return {}, None, None, None
    row = upstream.get(source_id, artifact)
    path = upstream.cached_path(source_id, artifact)
    if path is None:
        return row, None, None, None
    try:
        raw = path.read_bytes()
    except OSError:
        return row, None, None, None
    digest = hashlib.sha256(raw).hexdigest()
    expected = str(row.get("sha256") or row.get("signature") or "")
    return (row, path, raw, digest) if not expected or expected == digest else (row, None, None, None)


def _read(response, cap: int | None) -> bytes:
    if cap is None:
        return response.content
    try:
        declared = int(response.headers.get("content-length") or 0) or None
    except (TypeError, ValueError):
        declared = None
    if declared and declared > cap:
        raise LimitExceeded(response.url, cap, declared)
    size = 0
    chunks: list[bytes] = []
    for chunk in response.iter_content(chunk_size=256 * 1024):
        if not chunk:
            continue
        size += len(chunk)
        if size > cap:
            raise LimitExceeded(response.url, cap)
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch(session, *, url: str, source_id: str, artifact: str, upstream: UpstreamState | None, snapshot_dir: Path | None, user_agent: str, accept: str, method: str, committed: str | None = None, cap: int | None = None, replay: bool = False):
    row, path, cached_raw, cached_digest = _cached(upstream, source_id, artifact)
    if replay and not committed and cached_raw is not None and path is not None:
        response = SimpleNamespace(status_code=200, url=str(row.get("url") or url), headers={"content-type": row.get("content_type") or "", "etag": row.get("etag"), "last-modified": row.get("last_modified")})
        return response, cached_raw, cached_digest, path, True, False
    headers = {"User-Agent": user_agent, "Accept": accept}
    if upstream and ((committed and row.get("signature") == committed) or cached_raw is not None):
        headers.update(upstream.conditional_headers(source_id, artifact))
    kwargs: dict[str, Any] = {"headers": headers, "timeout": 180, "allow_redirects": True}
    if cap is not None:
        kwargs["stream"] = True
    response = session.get(url, **kwargs)
    if response.status_code == 304:
        if committed and row.get("signature") == committed:
            if upstream:
                upstream.mark_http_unchanged(source_id, artifact, url=response.url, extra={"signature": committed, "local_path": str(path) if path else None})
            return response, None, committed, path, False, True
        if cached_raw is not None and cached_digest and path:
            if upstream:
                upstream.mark_http_unchanged(source_id, artifact, url=response.url, extra={"signature": cached_digest, "local_path": str(path)})
            return response, cached_raw, cached_digest, path, True, True
        response = session.get(url, headers={"User-Agent": user_agent, "Accept": accept}, timeout=180, allow_redirects=True, stream=cap is not None)
    response.raise_for_status()
    raw = _read(response, cap)
    digest = hashlib.sha256(raw).hexdigest()
    if committed == digest:
        if upstream:
            upstream.mark_unchanged(source_id, artifact, signature=digest, url=response.url, reason="SHA256", etag=response.headers.get("etag"), last_modified=response.headers.get("last-modified"), extra={"content_type": response.headers.get("content-type")})
        return response, None, digest, path, False, False
    if cached_digest == digest and path is not None:
        local = path
    else:
        snap = save_snapshot(snapshot_dir, source_id=source_id, url=response.url, content=raw, content_type=response.headers.get("content-type"), name=f"{method.lower()}-{hashlib.sha256(url.encode()).hexdigest()[:16]}")
        local = Path(str(snap["local_path"])) if snap.get("local_path") else None
    if upstream:
        upstream.mark_downloaded(source_id, artifact, url=response.url, signature=digest, sha256=digest, size_bytes=len(raw), etag=response.headers.get("etag"), last_modified=response.headers.get("last-modified"), method=method, local_path=str(local) if local else None, extra={"content_type": response.headers.get("content-type")})
    return response, raw, digest, local, False, False


def _root(session, url: str, ua: str) -> str:
    try:
        r = session.head(url, headers={"User-Agent": ua}, timeout=60, allow_redirects=True)
        if r.status_code < 400:
            return _url(r.url)
    except Exception:
        pass
    r = session.get(url, headers={"User-Agent": ua}, timeout=120, allow_redirects=True, stream=True)
    r.raise_for_status()
    final = _url(r.url)
    r.close()
    return final


def _indexes(final_root: str, explicit: Iterable[str]) -> list[str]:
    if explicit:
        return [_url(urljoin(final_root, str(v))) for v in explicit if str(v).strip()]
    p = urlparse(final_root)
    origin = f"{p.scheme}://{p.netloc}"
    path = p.path if p.path.endswith("/") else p.path.rsplit("/", 1)[0] + "/"
    base = origin + path.rstrip("/")
    return list(dict.fromkeys([base + "/llms.txt", origin + "/llms.txt", base + "/sitemap.xml", origin + "/sitemap.xml"]))


def _discover(session, *, source_id: str, final_root: str, explicit: Iterable[str], upstream: UpstreamState | None, snapshot_dir: Path | None, ua: str, max_sitemaps: int, max_total_bytes: int):
    queue = deque(_indexes(final_root, explicit))
    p = urlparse(final_root)
    origin = f"{p.scheme}://{p.netloc}"
    try:
        robots = session.get(origin + "/robots.txt", headers={"User-Agent": ua}, timeout=30)
        if robots.ok:
            for line in robots.text.splitlines():
                key, sep, value = line.partition(":")
                if sep and key.strip().casefold() == "sitemap":
                    queue.append(_url(value.strip()))
    except Exception:
        pass
    pages: dict[str, Entry] = {}
    seen: set[str] = set()
    methods: set[str] = set()
    successful: list[str] = []
    remaining = max(0, int(max_total_bytes))
    downloaded_bytes = 0
    budget_truncated = False
    while queue and len(seen) < max_sitemaps:
        index = _url(queue.popleft())
        if index in seen:
            continue
        artifact = "discovery:" + hashlib.sha256(index.encode()).hexdigest()[:24]
        _, cached_path, cached_raw, _ = _cached(upstream, source_id, artifact)
        replay = cached_raw is not None and cached_path is not None
        if not replay and remaining <= 0:
            budget_truncated = True
            break
        seen.add(index)
        cap = 50_000_000 if replay else min(50_000_000, max(1, remaining))
        try:
            r, raw, _, path, replayed, _ = _fetch(session, url=index, source_id=source_id, artifact=artifact, upstream=upstream, snapshot_dir=snapshot_dir, user_agent=ua, accept="application/xml,text/xml,text/plain,text/markdown,text/html,*/*;q=0.1", method="DOCS_DISCOVERY_INDEX", cap=cap, replay=replay)
            if raw is None and path:
                raw = path.read_bytes()
            if raw is None:
                continue
            if not replayed:
                downloaded_bytes += len(raw)
                remaining = max(0, remaining - len(raw))
            ctype = (r.headers.get("content-type") or "").lower()
            try:
                found, nested = _sitemap(raw)
            except Exception:
                found, nested = [], []
            if found or nested:
                methods.add("sitemap")
                successful.append(index)
                for item in found:
                    old = pages.get(item.url)
                    if old is None or (item.lastmod and not old.lastmod):
                        pages[item.url] = item
                queue.extend(_url(urljoin(index, x)) for x in nested)
                continue
            found = _llms(raw, index)
            if found:
                methods.add("llms_txt")
                successful.append(index)
                for item in found:
                    pages.setdefault(item.url, item)
                continue
            if "html" in ctype or raw.lstrip().startswith(b"<"):
                _, hrefs = html_text_and_links(raw)
                if hrefs:
                    methods.add("html_index")
                    successful.append(index)
                    for href in hrefs:
                        page = _url(urljoin(index, href))
                        pages.setdefault(page, Entry(page))
        except LimitExceeded:
            if cap < 50_000_000:
                budget_truncated = True
                break
        except Exception:
            continue
    truncated = budget_truncated or bool(queue and len(seen) >= max_sitemaps)
    return list(pages.values()), {"methods": sorted(methods), "indexes_checked": len(seen), "successful_indexes": successful, "truncated": truncated, "downloaded_bytes": downloaded_bytes, "remaining_bytes": remaining}


def _crawl(session, *, source_id: str, root: str, host: str, prefixes: list[str], excludes: list[re.Pattern[str]], ua: str, max_pages: int, max_page_bytes: int, max_total_bytes: int, upstream: UpstreamState | None, snapshot_dir: Path | None):
    queue = deque([root])
    seen: set[str] = set()
    found: list[Entry] = []
    robots: dict[str, Any] = {}
    remaining = max(0, int(max_total_bytes))
    downloaded_bytes = 0
    budget_truncated = False
    while queue and len(seen) < max_pages:
        current = _url(queue.popleft())
        if current in seen or not _scope(current, host, prefixes, excludes):
            continue
        artifact = f"page:{current}"
        _, cached_path, cached_raw, _ = _cached(upstream, source_id, artifact)
        replay = cached_raw is not None and cached_path is not None
        if not replay and remaining <= 0:
            budget_truncated = True
            break
        seen.add(current)
        if not _robots_allowed(session, current, ua, robots):
            continue
        cap = max_page_bytes if replay else min(max_page_bytes, max(1, remaining))
        try:
            r, raw, _, path, replayed, _ = _fetch(session, url=current, source_id=source_id, artifact=artifact, upstream=upstream, snapshot_dir=snapshot_dir, user_agent=ua, accept="text/html,application/xhtml+xml,text/markdown,text/plain,*/*;q=0.1", method="DOCS_CRAWL_DISCOVERY", cap=cap, replay=replay)
            if raw is None and path:
                raw = path.read_bytes()
            if raw is None:
                continue
            if not replayed:
                downloaded_bytes += len(raw)
                remaining = max(0, remaining - len(raw))
            found.append(Entry(current))
            ctype = (r.headers.get("content-type") or "").lower()
            if "html" not in ctype and not raw.lstrip().startswith(b"<"):
                continue
            _, hrefs = html_text_and_links(raw)
            for href in hrefs:
                candidate = _url(urljoin(r.url, href))
                if candidate not in seen and _scope(candidate, host, prefixes, excludes):
                    queue.append(candidate)
        except LimitExceeded:
            if cap < max_page_bytes:
                budget_truncated = True
                break
        except Exception:
            continue
    return found, budget_truncated or bool(queue), downloaded_bytes, remaining


def _text(raw: bytes, ctype: str, url: str) -> str:
    lower = url.lower().split("?", 1)[0]
    ctype = (ctype or "").lower()
    if "pdf" in ctype or lower.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            return clean_text("\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(raw)).pages))
        except Exception:
            return ""
    if "html" in ctype or raw.lstrip().startswith(b"<"):
        return html_text_and_links(raw)[0]
    if "text/" in ctype or "markdown" in ctype or "json" in ctype or lower.endswith((".md", ".txt")):
        return clean_text(raw.decode("utf-8", "replace"))
    return ""


def official_docs_resource(*, source_id: str, url: str, user_agent: str = "IvoireData/0.8.3", discovery_urls: Iterable[str] = (), include_prefixes: Iterable[str] = (), exclude_patterns: Iterable[str] = (), max_pages: int = 100_000, max_sitemaps: int = 1_000, max_bytes_per_page: int = 12_000_000, max_new_bytes_per_run: int = 500_000_000, request_pause_seconds: float = 0.02, allow_crawl_fallback: bool = True, snapshot_dir: Path | None = None, metadata_base: dict[str, Any] | None = None, upstream_state_path: Path | None = None, license_name: str | None = None, license_url: str | None = None, training_eligible: bool = False, license_review_status: str = "UNREVIEWED"):
    import dlt, requests
    max_pages = max(1, int(max_pages))
    max_sitemaps = max(1, int(max_sitemaps))
    max_bytes_per_page = max(100_000, int(max_bytes_per_page))
    max_new_bytes_per_run = max(0, int(max_new_bytes_per_run))
    pause = max(0.0, min(float(request_pause_seconds), 2.0))
    base = dict(metadata_base or {})
    configured = [str(v) for v in include_prefixes if str(v)]
    excludes = [re.compile(v, re.I) for v in (*_EXCLUDES, *tuple(exclude_patterns))]

    @dlt.resource(name="official_docs", write_disposition="merge", primary_key="record_id")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent})
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None
        state = dlt.current.resource_state()
        hashes = state.setdefault("content_hashes_v1", {})
        lastmods = state.setdefault("sitemap_lastmods_v1", {})
        previous = set(str(v) for v in state.get("known_urls_v1", []) if v)
        final_root = _root(session, url, user_agent)
        host = (urlparse(final_root).hostname or "").lower()
        if not host:
            raise RuntimeError(f"official docs root has no host: {final_root}")
        prefixes = list(configured)
        if not prefixes:
            path = urlparse(final_root).path or "/"
            prefixes = [path if path.endswith("/") else (path.rsplit("/", 1)[0] + "/" if "/" in path else "/")]

        entries, discovery = _discover(session, source_id=source_id, final_root=final_root, explicit=discovery_urls, upstream=upstream, snapshot_dir=snapshot_dir, ua=user_agent, max_sitemaps=max_sitemaps, max_total_bytes=max_new_bytes_per_run)
        methods = set(discovery["methods"])
        authoritative = "sitemap" in methods
        discovery_bytes = int(discovery.get("downloaded_bytes") or 0)
        remaining_discovery_budget = int(discovery.get("remaining_bytes") or 0)
        crawl_truncated = False
        fallback = False
        if allow_crawl_fallback and not authoritative and remaining_discovery_budget > 0:
            crawled, crawl_truncated, crawl_bytes, remaining_discovery_budget = _crawl(session, source_id=source_id, root=final_root, host=host, prefixes=prefixes, excludes=excludes, ua=user_agent, max_pages=max_pages, max_page_bytes=max_bytes_per_page, max_total_bytes=remaining_discovery_budget, upstream=upstream, snapshot_dir=snapshot_dir)
            discovery_bytes += crawl_bytes
            fallback = True
            methods.add("crawl_fallback")
            merged = {e.url: e for e in entries}
            for item in crawled:
                merged.setdefault(item.url, item)
            entries = list(merged.values())
        elif allow_crawl_fallback and not authoritative:
            fallback = True
            crawl_truncated = True
            methods.add("crawl_fallback")

        scoped: dict[str, Entry] = {}
        for e in entries:
            page = _url(e.url)
            if _scope(page, host, prefixes, excludes):
                old = scoped.get(page)
                if old is None or (e.lastmod and not old.lastmod):
                    scoped[page] = Entry(page, e.lastmod)
        all_pages = sorted(scoped.values(), key=lambda e: e.url)
        truncated = bool(discovery.get("truncated")) or crawl_truncated or len(all_pages) > max_pages
        selected = all_pages[:max_pages]
        complete = bool(selected) and not truncated and (authoritative or (fallback and not crawl_truncated))
        stats: dict[str, Any] = {"source_id": source_id, "root_url": url, "final_root_url": final_root, "discovery_methods": sorted(methods), "discovery_indexes_checked": discovery["indexes_checked"], "successful_discovery_indexes": discovery["successful_indexes"], "discovery_fallback": fallback, "authoritative_sitemap": authoritative, "discovery_complete": complete, "discovery_truncated": truncated, "discovery_downloaded_bytes": discovery_bytes, "discovered_pages": len(scoped), "selected_pages": len(selected), "unchanged_lastmod": 0, "unchanged_http304": 0, "unchanged_sha256": 0, "downloaded": 0, "replayed_from_local_cache": 0, "failed": 0, "skipped_oversize": 0, "deferred_budget": 0, "downloaded_bytes": 0, "business_chunks": 0, "removed_upstream": 0, "backlog_count": 0, "failures": [], "license_name": license_name, "license_url": license_url, "license_review_status": license_review_status, "training_eligible": bool(training_eligible)}
        if not selected:
            raise RuntimeError(f"no official documentation pages discovered for {source_id}")
        current = {e.url for e in selected}
        if complete:
            removed = sorted(previous - current)
            stats["removed_upstream"] = len(removed)
            for page in removed:
                rid = hashlib.sha256(f"{source_id}|page|{page}".encode()).hexdigest()
                yield dlt.mark.with_table_name({"record_id": rid, "source_id": source_id, "page_url": page, "active": False, "content_sha256": hashes.get(page), "lastmod": lastmods.get(page), "license_name": license_name, "license_url": license_url, "license_review_status": license_review_status, "training_eligible": bool(training_eligible)}, "official_docs_pages")
                if upstream:
                    upstream.mark_removed(source_id, f"page:{page}")

        budget = max(0, max_new_bytes_per_run - discovery_bytes)
        robots: dict[str, Any] = {}
        for e in selected:
            page = e.url
            committed = hashes.get(page)
            if committed and e.lastmod and lastmods.get(page) == e.lastmod:
                stats["unchanged_lastmod"] += 1
                if upstream:
                    upstream.mark_unchanged(source_id, f"page:{page}", signature=committed, url=page, reason="SITEMAP_LASTMOD", extra={"lastmod": e.lastmod})
                continue
            if not _robots_allowed(session, page, user_agent, robots):
                stats["failed"] += 1
                stats["failures"].append({"url": page, "error": "ROBOTS_DISALLOWED"})
                continue
            artifact = f"page:{page}"
            cached_row, cached_path, cached_raw, _ = _cached(upstream, source_id, artifact)
            replay = not committed and cached_raw is not None and cached_path is not None
            if not replay and budget <= 0:
                stats["deferred_budget"] += 1
                continue
            cap = max_bytes_per_page if replay else min(max_bytes_per_page, max(1, budget))
            try:
                r, raw, digest, local, replayed, http304 = _fetch(session, url=page, source_id=source_id, artifact=artifact, upstream=upstream, snapshot_dir=snapshot_dir, user_agent=user_agent, accept="application/pdf,text/markdown,text/html,application/xhtml+xml,text/plain,*/*;q=0.1", method="OFFICIAL_DOC_PAGE", committed=committed, cap=cap, replay=replay)
                if raw is None:
                    stats["unchanged_http304" if http304 else "unchanged_sha256"] += 1
                    if e.lastmod:
                        lastmods[page] = e.lastmod
                    continue
                if not replayed:
                    budget = max(0, budget - len(raw))
                    stats["downloaded"] += 1
                    stats["downloaded_bytes"] += len(raw)
                else:
                    stats["replayed_from_local_cache"] += 1
                ctype = str(r.headers.get("content-type") or cached_row.get("content_type") or "")
                text = _text(raw, ctype, page)
                if not text:
                    stats["failed"] += 1
                    stats["failures"].append({"url": page, "error": "NO_EXTRACTABLE_TEXT"})
                    continue
                classified = classify_from_base(base, page, text, document_type="DEVELOPER_DOCUMENTATION")
                title = title_from_text(text)
                page_id = hashlib.sha256(f"{source_id}|page|{page}".encode()).hexdigest()
                yield dlt.mark.with_table_name({"record_id": page_id, "source_id": source_id, "page_url": page, "document_title": title, "active": True, "content_sha256": digest, "lastmod": e.lastmod, "content_type": ctype or None, "local_snapshot": str(local) if local else None, "license_name": license_name, "license_url": license_url, "license_review_status": license_review_status, "training_eligible": bool(training_eligible), **classified}, "official_docs_pages")
                emitted = 0
                for idx, chunk in enumerate(chunk_text(text, size=5000, overlap=300)):
                    chunk_id = hashlib.sha256(f"{source_id}|{page}|{digest}|{idx}".encode()).hexdigest()
                    emitted += 1
                    yield dlt.mark.with_table_name({"record_id": chunk_id, "chunk_id": chunk_id, "page_record_id": page_id, "source_id": source_id, "source_url": page, "page_url": page, "document_title": title, "content_sha256": digest, "chunk_index": idx, "content_type": ctype or None, "local_snapshot": str(local) if local else None, "text": chunk, "active_at_ingest": True, "license_name": license_name, "license_url": license_url, "license_review_status": license_review_status, "training_eligible": bool(training_eligible), **classified}, "official_docs_chunks")
                stats["business_chunks"] += emitted
                hashes[page] = digest
                if e.lastmod:
                    lastmods[page] = e.lastmod
                if pause:
                    time.sleep(pause)
            except LimitExceeded as exc:
                if cap < max_bytes_per_page:
                    stats["deferred_budget"] += 1
                else:
                    stats["skipped_oversize"] += 1
                    stats["failures"].append({"url": page, "error": "PAGE_TOO_LARGE", "limit": exc.limit, "content_length": exc.declared})
            except Exception as exc:
                stats["failed"] += 1
                status = getattr(getattr(exc, "response", None), "status_code", None)
                stats["failures"].append({"url": page, "status": status, "error": str(exc)[:1000]})
                if upstream:
                    upstream.mark_error(source_id, artifact, url=page, error=str(exc), status_code=status, method="OFFICIAL_DOC_PAGE")
        state["known_urls_v1"] = sorted(current if complete else previous | current)
        stats["backlog_count"] = stats["deferred_budget"] + stats["skipped_oversize"] + stats["failed"] + int(not complete)
        if snapshot_dir:
            atomic_write_json(snapshot_dir / "official_docs_sync_stats.json", stats)
        rid = hashlib.sha256(f"{source_id}|official-docs-stats".encode()).hexdigest()
        yield dlt.mark.with_table_name({"record_id": rid, "run_stats_json": json.dumps(stats, ensure_ascii=False), **stats}, "official_docs_sync_stats")
    return resource()
