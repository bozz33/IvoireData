from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin


_DOWNLOAD_RE = re.compile(r"\.(csv|tsv|json|jsonl|xml|xlsx?|parquet|zip|gz)(?:$|\?)", re.I)


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links: list[tuple[str, str]] = []; self.href: str | None = None; self.text: list[str] = []
    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.href = dict(attrs).get("href"); self.text = []
    def handle_data(self, data):
        if self.href: self.text.append(data)
    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.href:
            self.links.append((self.href, " ".join(self.text).strip())); self.href = None; self.text = []


def bulk_catalog_resource(*, source_id: str, page_url: str, user_agent: str = "IvoireData/0.5", download_dir: Path | None = None, download_patterns: list[str] | None = None, max_downloads: int = 0, max_bytes: int = 250_000_000):
    """Discover official bulk-download links and optionally snapshot selected files.

    By default this connector stores the current catalog only. Downloads are
    opt-in with regex patterns so a scheduled run cannot accidentally pull a
    multi-gigabyte corpus.
    """
    import dlt
    import requests

    patterns = [re.compile(p, re.I) for p in (download_patterns or [])]

    @dlt.resource(name="bulk_catalog", write_disposition="replace")
    def resource():
        session = requests.Session(); session.headers.update({"User-Agent": user_agent})
        response = session.get(page_url, timeout=120); response.raise_for_status()
        parser = _Links(); parser.feed(response.text)
        downloads = 0
        for href, label in parser.links:
            absolute = urljoin(page_url, href)
            is_download = bool(_DOWNLOAD_RE.search(absolute))
            row = {"source_id": source_id, "catalog_url": page_url, "url": absolute, "label": label, "download_candidate": is_download}
            should_download = bool(patterns) and is_download and any(p.search(absolute) or p.search(label) for p in patterns)
            if should_download and (max_downloads <= 0 or downloads < max_downloads) and download_dir is not None:
                head = session.head(absolute, timeout=60, allow_redirects=True)
                length = int(head.headers.get("content-length") or 0)
                if length and length > max_bytes:
                    row["snapshot_status"] = "skipped_too_large"; row["content_length"] = length
                else:
                    payload = session.get(absolute, timeout=300); payload.raise_for_status()
                    if len(payload.content) > max_bytes:
                        row["snapshot_status"] = "skipped_too_large"; row["content_length"] = len(payload.content)
                    else:
                        download_dir.mkdir(parents=True, exist_ok=True)
                        filename = absolute.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or f"download-{downloads}"
                        path = download_dir / filename; path.write_bytes(payload.content)
                        row.update({"snapshot_status": "saved", "local_path": str(path), "sha256": hashlib.sha256(payload.content).hexdigest(), "content_length": len(payload.content)})
                        downloads += 1
            yield row

    return resource()
