from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .state_io import atomic_write_json

_SAFE = re.compile(r"[^a-zA-Z0-9_.-]+")
_SAFE_COMPONENT_MAX = 120


def _safe_filename(value: str, *, max_length: int = _SAFE_COMPONENT_MAX) -> str:
    """Return an ASCII-safe path component with a bounded byte length.

    Linux filesystems commonly cap a single filename at 255 bytes. Snapshot names also
    append ``--<sha256>`` and an extension, so allowing a 180-byte stem could exceed
    NAME_MAX for long Data Fair dataset identifiers. Keep the human-readable prefix
    bounded and let the digest provide collision resistance.
    """
    value = _SAFE.sub("_", value).strip("._")
    limit = max(16, int(max_length))
    return value[:limit] or "payload"


def _extension(url: str, content_type: str | None, name: str | None = None) -> str:
    if name and Path(name).suffix:
        suffix = Path(name).suffix
        return suffix[:16] if len(suffix) > 16 else suffix
    suffix = Path(urlparse(url).path).suffix
    if suffix and len(suffix) <= 12:
        return suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if guessed:
            return guessed
    return ".bin"


def new_temp_path(directory: Path, *, prefix: str = "artifact") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{_safe_filename(prefix)}-", suffix=".part", dir=directory)
    os.close(fd)
    return Path(raw)


def finalize_temp_snapshot(
    directory: Path,
    *,
    temp_path: Path,
    source_id: str,
    url: str,
    content_type: str | None = None,
    name: str | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
) -> dict[str, object]:
    """Atomically promote a completed temporary file to a digest-addressed snapshot."""
    directory.mkdir(parents=True, exist_ok=True)
    temp_path = Path(temp_path)
    digest = hashlib.sha256()
    measured = 0
    if sha256 is None or size_bytes is None:
        with temp_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                measured += len(chunk)
                digest.update(chunk)
        actual_sha = digest.hexdigest()
        actual_size = measured
    else:
        actual_sha = str(sha256)
        actual_size = int(size_bytes)

    base = name or Path(urlparse(url).path).name or source_id
    ext = _extension(url, content_type, name)
    if not Path(base).suffix:
        base += ext
    suffix = Path(base).suffix or ext
    stem = _safe_filename(Path(base).stem)
    filename = f"{stem}--{actual_sha}{suffix}"
    # Defensive guard independent from filesystem NAME_MAX. The normal bound above is
    # already well below 255 bytes, but preserve the digest even with exotic suffixes.
    if len(filename.encode("utf-8")) > 240:
        stem = _safe_filename(stem, max_length=80)
        filename = f"{stem}--{actual_sha}{suffix[:16]}"
    final_path = directory / filename
    if final_path.exists():
        temp_path.unlink(missing_ok=True)
    else:
        os.replace(temp_path, final_path)

    meta = {
        "source_id": source_id,
        "source_url": url,
        "content_type": content_type,
        "sha256": actual_sha,
        "size_bytes": actual_size,
        "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "local_file": filename,
        "write_mode": "streaming",
    }
    atomic_write_json(final_path.with_suffix(final_path.suffix + ".meta.json"), meta)
    return {
        "sha256": actual_sha,
        "size_bytes": actual_size,
        "source_url": url,
        "local_path": str(final_path),
        "content_type": content_type,
    }


def stream_response_snapshot(
    response: Any,
    directory: Path,
    *,
    source_id: str,
    url: str,
    content_type: str | None = None,
    name: str | None = None,
    chunk_size: int = 1024 * 1024,
    expected_sha256: str | None = None,
    max_bytes: int | None = None,
) -> dict[str, object]:
    """Write response bytes incrementally and promote only validated complete content.

    ``expected_sha256`` is useful for physical repair: historical bytes are restored only
    when the upstream URL still serves the exact object previously recorded. ``max_bytes``
    prevents a stale/misconfigured URL from filling local storage during a repair.
    """
    temp_path = new_temp_path(directory, prefix=name or source_id)
    digest = hashlib.sha256()
    size = 0
    expected = str(expected_sha256 or "").strip().lower() or None
    byte_limit = int(max_bytes) if max_bytes is not None else None
    try:
        with temp_path.open("wb") as handle:
            iterator: Iterable[bytes] = response.iter_content(chunk_size=max(64 * 1024, int(chunk_size)))
            for chunk in iterator:
                if not chunk:
                    continue
                size += len(chunk)
                if byte_limit is not None and size > byte_limit:
                    raise ValueError(f"payload too large: {size} > {byte_limit}")
                handle.write(chunk)
                digest.update(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        actual_sha = digest.hexdigest()
        if expected is not None and actual_sha.lower() != expected:
            raise ValueError(
                f"sha256 mismatch during repair expected={expected} actual={actual_sha}"
            )
        return finalize_temp_snapshot(
            directory,
            temp_path=temp_path,
            source_id=source_id,
            url=url,
            content_type=content_type,
            name=name,
            sha256=actual_sha,
            size_bytes=size,
        )
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
