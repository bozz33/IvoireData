from __future__ import annotations

import hashlib
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .state_io import atomic_write_json

_SAFE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _safe_filename(value: str) -> str:
    value = _SAFE.sub("_", value).strip("._")
    return value[:180] or "payload"


def _extension(url: str, content_type: str | None) -> str:
    suffix = Path(urlparse(url).path).suffix
    if suffix and len(suffix) <= 12:
        return suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if guessed:
            return guessed
    return ".bin"


def save_snapshot(
    directory: Path | None,
    *,
    source_id: str,
    url: str,
    content: bytes,
    content_type: str | None = None,
    name: str | None = None,
) -> dict[str, object]:
    digest = hashlib.sha256(content).hexdigest()
    result: dict[str, object] = {
        "sha256": digest,
        "size_bytes": len(content),
        "source_url": url,
    }
    if directory is None:
        return result
    directory.mkdir(parents=True, exist_ok=True)
    base = name or Path(urlparse(url).path).name or source_id
    ext = _extension(url, content_type)
    if not Path(base).suffix:
        base += ext
    filename = f"{_safe_filename(Path(base).stem)}--{digest[:16]}{Path(base).suffix or ext}"
    path = directory / filename
    if not path.exists():
        path.write_bytes(content)
    meta = {
        "source_id": source_id,
        "source_url": url,
        "content_type": content_type,
        "sha256": digest,
        "size_bytes": len(content),
        "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "local_file": filename,
    }
    sidecar = path.with_suffix(path.suffix + ".meta.json")
    atomic_write_json(sidecar, meta)
    result["local_path"] = str(path)
    return result
