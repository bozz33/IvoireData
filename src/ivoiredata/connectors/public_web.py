from __future__ import annotations

import hashlib
import io
import re
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from ..cleaning import clean_text
from ..metadata import classify_from_base, title_from_text
from ..snapshots import save_snapshot
from ..state_io import atomic_write_json
from ..upstream_state import UpstreamState

_SKIP_EXTENSIONS = {
    ".7z", ".avi", ".bin", ".bmp", ".css", ".doc", ".docx", ".dta", ".exe", ".gif",
    ".gz", ".ico", ".jpeg", ".jpg", ".js", ".mp3", ".mp4", ".odt", ".pbf",
    ".png", ".por", ".ppt", ".pptx", ".rar", ".rdata", ".rds", ".sas7bdat", ".sav",
    ".svg", ".tar", ".webp", ".woff", ".woff2", ".xpt", ".zip",
}
_METADATA_DENY_TOKENS = ("download", "microdata", "datafile", "data-file", "get-microdata", "get_microdata")
_MIN_PDF_TEXT_CHARS = 80
_ENCODED_TRAILING_WS = re.compile(r"(?:(?:%20)|(?:%09)|(?:%0a)|(?:%0d))+$", re.IGNORECASE)
_UPLOAD_DIRECTORY = re.compile(r"/(?:uploads?|wp-content/uploads)(?:/[^/?#]+)*/$", re.IGNORECASE)


class _HTMLTextAndLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.links: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        lower = tag.lower()
        if lower in {"script", "style", "noscript"}:
            self.skip += 1
        if lower == "a":
            for key, value in attrs:
                if key.lower() == "href" and value:
                    self.links.append(value)

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_text_and_links(raw: bytes) -> tuple[str, list[str]]:
    parser = _HTMLTextAndLinks()
    parser.feed(raw.decode("utf-8", "replace"))
    return clean_text("\n".join(parser.parts)), parser.links


def html_text(raw: bytes) -> str:
    return html_text_and_links(raw)[0]


def chunk_text(text: str, size: int = 3500, overlap: int = 250):
    text = clean_text(text)
    if not text:
        return
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        yield text[start:end]
        if end == len(text):
            break
        start = max(start + 1, end - overlap)


def _normalize_url(value: str) -> str:
    raw = str(value or "").strip()
    split = urlsplit(raw)
    path = _ENCODED_TRAILING_WS.sub("", split.path.rstrip())
    return urlunsplit((split.scheme, split.netloc, path, split.query, ""))


def _same_host_links(base_url: str, hrefs: list[str], *, metadata_only: bool = False) -> list[str]:
    base_url = _normalize_url(base_url)
    host = (urlparse(base_url).hostname or "").lower()
    out: list[str] = []
    for href in hrefs:
        raw_href = str(href or "").strip()
        if not raw_href:
            continue
        candidate = _normalize_url(urldefrag(urljoin(base_url, raw_href))[0])
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            continue
        if (parsed.hostname or "").lower() != host:
            continue
        path = parsed.path.lower()
        # Asset directories such as /uploads/publications/ are crawl containers, not
        # retrievable documents. Tracking them as artifacts creates permanent 403 noise.
        if _UPLOAD_DIRECTORY.search(path):
            continue
        if any(path.endswith(ext) for ext in _SKIP_EXTENSIONS):
            continue
        if metadata_only:
            target = (parsed.path + "?" + parsed.query).lower()
            if any(token in target for token in _METADATA_DENY_TOKENS):
                continue
        out.append(candidate)
    return list(dict.fromkeys(out))


def _robots_allowed(session, url: str, user_agent: str, cache: dict[str, RobotFileParser | None]) -> bool:
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    if root not in cache:
        robots_url = f"{root}/robots.txt"
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            response = session.get(robots_url, timeout=30, headers={"User-Agent": user_agent})
            if response.ok:
                parser.parse(response.text.splitlines())
                cache[root] = parser
            else:
                cache[root] = None
        except Exception:
            cache[root] = None
    parser = cache[root]
    return True if parser is None else parser.can_fetch(user_agent, url)


def _write_needs_ocr(snapshot: dict, *, source_id: str, source_url: str, sha256: str, text_chars: int) -> str | None:
    local = snapshot.get("local_path")
    if not local:
        return None
    sidecar = Path(str(local) + ".needs_ocr.json")
    try:
        atomic_write_json(sidecar, {
            "status": "NEEDS_OCR",
            "source_id": source_id,
            "source_url": source_url,
            "content_sha256": sha256,
            "extracted_text_chars": int(text_chars),
            "reason": "PDF contains too little extractable text; likely scanned/image-based or text extraction failed.",
            "automatic_ocr": False,
        })
    except OSError:
        return None
    return str(sidecar)


def _cached_body(upstream: UpstreamState | None, source_id: str, artifact: str, cached: dict):
    if upstream is None:
        return None, None
    path = upstream.cached_path(source_id, artifact)
    if path is None:
        return None, None
    try:
        raw = path.read_bytes()
    except OSError:
        return None, None
    digest = hashlib.sha256(raw).hexdigest()
    expected = str(cached.get("sha256") or cached.get("signature") or "").strip()
    if expected and expected != digest:
        return None, None
    return path, raw


def public_document_resource(
    *,
    source_id: str,
    url: str,
    user_agent: str = "IvoireData/0.8.4",
    force: bool = False,
    crawl: bool = False,
    max_pages: int = 1,
    max_bytes: int = 20_000_000,
    metadata_only: bool = False,
    snapshot_dir: Path | None = None,
    verify_ssl: bool = True,
    metadata_base: dict | None = None,
    upstream_state_path: Path | None = None,
):
    """Crawl public documents incrementally with crash-safe local replay.

    `force` means "check now", never "download identical bytes again". ETag and
    Last-Modified are used when the matching body is committed or when an integrity-
    checked local snapshot is available for replay. Thus a crash after body transfer but
    before dlt commit can be repaired from disk after a 304 without transferring the body
    a second time. Servers without validators are deduplicated by SHA-256.
    """
    import dlt
    import requests
    from pypdf import PdfReader

    max_pages = max(1, min(int(max_pages), 500))
    max_bytes = max(100_000, int(max_bytes))
    base = dict(metadata_base or {})
    url = _normalize_url(url)

    @dlt.resource(name="public_documents", write_disposition="merge", primary_key="chunk_id")
    def resource():
        state = dlt.current.resource_state().setdefault("content_hashes", {})
        session = requests.Session()
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None
        if not verify_ssl:
            session.verify = False
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass
            print(f"[public_web] {source_id}: TLS désactivé (certificat serveur invalide)", flush=True)

        queue = deque([url])
        seen: set[str] = set()
        robots_cache: dict[str, RobotFileParser | None] = {}
        fetched = 0
        while queue and fetched < max_pages:
            current = _normalize_url(queue.popleft())
            if current in seen:
                continue
            seen.add(current)
            if metadata_only:
                parsed_current = urlparse(current)
                target = (parsed_current.path + "?" + parsed_current.query).lower()
                if current != url and any(token in target for token in _METADATA_DENY_TOKENS):
                    continue
            if not _robots_allowed(session, current, user_agent, robots_cache):
                continue

            artifact = f"url:{current}"
            cached = upstream.get(source_id, artifact) if upstream else {}
            committed_digest = state.get(current)
            cached_path, cached_raw = _cached_body(upstream, source_id, artifact, cached)
            headers = {"User-Agent": user_agent}
            if upstream and (
                (committed_digest and cached.get("signature") == committed_digest)
                or cached_raw is not None
            ):
                headers.update(upstream.conditional_headers(source_id, artifact))

            replayed_from_cache = False
            response = None
            raw: bytes
            response_url = current
            response_headers: dict = {}
            ctype = ""
            try:
                response = session.get(current, timeout=120, headers=headers)
                if response.status_code == 304:
                    if committed_digest and cached.get("signature") == committed_digest:
                        fetched += 1
                        cached_links = list(cached.get("cached_links") or [])
                        if upstream:
                            upstream.mark_http_unchanged(
                                source_id, artifact, url=current,
                                extra={"signature": committed_digest, "cached_links": cached_links},
                            )
                        for candidate in cached_links:
                            if candidate not in seen:
                                queue.append(str(candidate))
                        continue
                    if cached_raw is not None and cached_path is not None:
                        raw = cached_raw
                        replayed_from_cache = True
                        response_url = str(cached.get("url") or current)
                        ctype = str(cached.get("content_type") or "").lower()
                        response_headers = {
                            "etag": cached.get("etag"),
                            "last-modified": cached.get("last_modified"),
                        }
                    else:
                        response = session.get(current, timeout=120, headers={"User-Agent": user_agent})
                        response.raise_for_status()
                else:
                    response.raise_for_status()
            except requests.exceptions.RequestException as exc:
                if upstream:
                    upstream.mark_error(
                        source_id, artifact, url=current, error=str(exc),
                        status_code=getattr(getattr(exc, "response", None), "status_code", None),
                        method="HTTP_DOCUMENT",
                    )
                if current == url:
                    raise
                print(f"[public_web] {source_id}: lien ignoré {current} -> {exc}", flush=True)
                continue

            fetched += 1
            if not replayed_from_cache:
                assert response is not None
                raw = response.content
                response_url = _normalize_url(response.url)
                response_headers = dict(response.headers)
                ctype = response.headers.get("content-type", "").lower()

            if len(raw) > max_bytes:
                if upstream:
                    upstream.mark_error(source_id, artifact, url=current, error=f"payload too large: {len(raw)} > {max_bytes}", method="HTTP_DOCUMENT")
                continue
            digest = hashlib.sha256(raw).hexdigest()
            text = ""
            links: list[str] = []
            is_pdf = "pdf" in ctype or current.lower().split("?", 1)[0].endswith(".pdf")
            if is_pdf:
                reader = PdfReader(io.BytesIO(raw))
                text = "\n".join((page.extract_text() or "") for page in reader.pages)
            elif "html" in ctype or raw.lstrip().startswith(b"<"):
                text, hrefs = html_text_and_links(raw)
                if crawl:
                    links = _same_host_links(current, hrefs, metadata_only=metadata_only)
            elif any(token in ctype for token in ("json", "xml", "text/")):
                text = clean_text(raw.decode("utf-8", "replace"))
            else:
                continue

            if committed_digest == digest:
                cached_links = links or list(cached.get("cached_links") or [])
                if upstream:
                    upstream.mark_unchanged(
                        source_id, artifact, signature=digest, url=response_url, reason="SHA256",
                        etag=response_headers.get("etag"), last_modified=response_headers.get("last-modified"),
                        extra={"cached_links": cached_links, "content_type": ctype or None},
                    )
                for candidate in cached_links:
                    if candidate not in seen:
                        queue.append(str(candidate))
                continue

            text = clean_text(text)
            extraction_status = "NEEDS_OCR" if is_pdf and len(text) < _MIN_PDF_TEXT_CHARS else "TEXT_EXTRACTED"
            if replayed_from_cache and cached_path is not None:
                snapshot = {
                    "sha256": digest,
                    "size_bytes": len(raw),
                    "source_url": response_url,
                    "local_path": str(cached_path),
                }
            else:
                snapshot = save_snapshot(snapshot_dir, source_id=source_id, url=current, content=raw, content_type=ctype or None)
            needs_ocr_sidecar = None
            if extraction_status == "NEEDS_OCR":
                needs_ocr_sidecar = _write_needs_ocr(
                    snapshot, source_id=source_id, source_url=current, sha256=digest, text_chars=len(text)
                )
            classified = classify_from_base(base, current, text)
            document_title = title_from_text(text)
            emitted = 0
            for idx, chunk in enumerate(chunk_text(text)):
                chunk_id = hashlib.sha256(f"{source_id}|{current}|{digest}|{idx}".encode()).hexdigest()
                emitted += 1
                yield {
                    "chunk_id": chunk_id,
                    "source_id": source_id,
                    "source_url": current,
                    "document_title": document_title,
                    "content_sha256": digest,
                    "content_type": ctype or None,
                    "local_snapshot": snapshot.get("local_path"),
                    "chunk_index": idx,
                    "metadata_only": metadata_only,
                    "extraction_status": extraction_status,
                    "needs_ocr_sidecar": needs_ocr_sidecar,
                    "text": chunk,
                    **classified,
                }

            state[current] = digest
            if upstream:
                upstream.mark_downloaded(
                    source_id, artifact,
                    url=response_url,
                    signature=digest,
                    sha256=digest,
                    size_bytes=len(raw),
                    etag=response_headers.get("etag"),
                    last_modified=response_headers.get("last-modified"),
                    method="CACHE_REPLAY_AFTER_304" if replayed_from_cache else "HTTP_DOCUMENT",
                    rows=emitted,
                    local_path=str(snapshot.get("local_path") or "") or None,
                    extra={"cached_links": links, "content_type": ctype or None, "body_changed": True},
                )

            for candidate in links:
                if candidate not in seen:
                    queue.append(candidate)

    return resource()
