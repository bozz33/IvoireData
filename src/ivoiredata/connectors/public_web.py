from __future__ import annotations

import hashlib
import io
from collections import deque
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

from ..cleaning import clean_text

_SKIP_EXTENSIONS = {
    ".7z", ".avi", ".bin", ".bmp", ".css", ".doc", ".docx", ".exe", ".gif",
    ".gz", ".ico", ".jpeg", ".jpg", ".js", ".mp3", ".mp4", ".odt", ".pbf",
    ".png", ".ppt", ".pptx", ".rar", ".svg", ".tar", ".webp", ".woff",
    ".woff2", ".zip",
}


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


def _same_host_links(base_url: str, hrefs: list[str]) -> list[str]:
    host = (urlparse(base_url).hostname or "").lower()
    out: list[str] = []
    for href in hrefs:
        candidate = urldefrag(urljoin(base_url, href))[0]
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            continue
        if (parsed.hostname or "").lower() != host:
            continue
        path = parsed.path.lower()
        if any(path.endswith(ext) for ext in _SKIP_EXTENSIONS):
            continue
        out.append(candidate)
    return out


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


def public_document_resource(*, source_id: str, url: str, user_agent: str = "IvoireData/0.4", force: bool = False, crawl: bool = False, max_pages: int = 1, max_bytes: int = 20_000_000):
    import dlt
    import requests
    from pypdf import PdfReader

    max_pages = max(1, min(int(max_pages), 500))
    max_bytes = max(100_000, int(max_bytes))

    @dlt.resource(name="public_documents", write_disposition="merge", primary_key="chunk_id")
    def resource():
        state = dlt.current.resource_state().setdefault("content_hashes", {})
        session = requests.Session()
        queue = deque([url])
        seen: set[str] = set()
        robots_cache: dict[str, RobotFileParser | None] = {}
        fetched = 0
        while queue and fetched < max_pages:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            if not _robots_allowed(session, current, user_agent, robots_cache):
                continue
            response = session.get(current, timeout=120, headers={"User-Agent": user_agent})
            response.raise_for_status()
            fetched += 1
            raw = response.content
            if len(raw) > max_bytes:
                continue
            digest = hashlib.sha256(raw).hexdigest()
            ctype = response.headers.get("content-type", "").lower()
            text = ""
            links: list[str] = []
            if "pdf" in ctype or current.lower().split("?", 1)[0].endswith(".pdf"):
                reader = PdfReader(io.BytesIO(raw))
                text = "\n".join((page.extract_text() or "") for page in reader.pages)
            elif "html" in ctype or raw.lstrip().startswith(b"<"):
                text, hrefs = html_text_and_links(raw)
                if crawl:
                    links = _same_host_links(current, hrefs)
            elif any(token in ctype for token in ("json", "xml", "text/")):
                text = clean_text(raw.decode("utf-8", "replace"))
            else:
                continue
            if force or state.get(current) != digest:
                for idx, chunk in enumerate(chunk_text(text)):
                    chunk_id = hashlib.sha256(f"{source_id}|{current}|{digest}|{idx}".encode()).hexdigest()
                    yield {"chunk_id": chunk_id, "source_id": source_id, "source_url": current, "content_sha256": digest, "chunk_index": idx, "text": chunk}
                state[current] = digest
            for candidate in links:
                if candidate not in seen:
                    queue.append(candidate)
    return resource()
