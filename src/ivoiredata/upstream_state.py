from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .state_io import atomic_write_json, load_json


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class UpstreamState:
    """Persistent cache metadata used to avoid transferring unchanged upstream data."""

    def __init__(self, path: Path):
        self.path = path
        payload = load_json(path, {"schema_version": 1, "resources": {}})
        if not isinstance(payload, dict):
            payload = {"schema_version": 1, "resources": {}}
        payload.setdefault("schema_version", 1)
        payload.setdefault("resources", {})
        self.data: dict[str, Any] = payload

    @staticmethod
    def key(source_id: str, artifact_id: str) -> str:
        return f"{source_id}::{artifact_id}"

    def get(self, source_id: str, artifact_id: str) -> dict[str, Any]:
        row = self.data["resources"].get(self.key(source_id, artifact_id), {})
        return dict(row) if isinstance(row, dict) else {}

    def signature_matches(self, source_id: str, artifact_id: str, signature: str | None) -> bool:
        if not signature:
            return False
        row = self.get(source_id, artifact_id)
        return bool(row.get("downloaded") and row.get("signature") == signature and not row.get("removed"))

    def conditional_headers(self, source_id: str, artifact_id: str) -> dict[str, str]:
        row = self.get(source_id, artifact_id)
        headers: dict[str, str] = {}
        if row.get("etag"):
            headers["If-None-Match"] = str(row["etag"])
        if row.get("last_modified"):
            headers["If-Modified-Since"] = str(row["last_modified"])
        return headers

    def _update(self, source_id: str, artifact_id: str, **values: Any) -> dict[str, Any]:
        key = self.key(source_id, artifact_id)
        row = self.data["resources"].setdefault(key, {})
        row.update(values)
        row["source_id"] = source_id
        row["artifact_id"] = artifact_id
        atomic_write_json(self.path, self.data)
        return dict(row)

    def mark_unchanged(self, source_id: str, artifact_id: str, *, signature: str | None = None,
                       url: str | None = None, reason: str = "signature", etag: str | None = None,
                       last_modified: str | None = None) -> dict[str, Any]:
        values: dict[str, Any] = {
            "last_checked": _now(),
            "last_result": "UNCHANGED",
            "unchanged_reason": reason,
            "removed": False,
            "error": None,
        }
        if signature is not None:
            values["signature"] = signature
        if url is not None:
            values["url"] = url
        if etag is not None:
            values["etag"] = etag
        if last_modified is not None:
            values["last_modified"] = last_modified
        return self._update(source_id, artifact_id, **values)

    def mark_downloaded(self, source_id: str, artifact_id: str, *, url: str, signature: str | None,
                        sha256: str | None, size_bytes: int | None, etag: str | None = None,
                        last_modified: str | None = None, method: str | None = None,
                        rows: int | None = None) -> dict[str, Any]:
        now = _now()
        return self._update(
            source_id, artifact_id,
            url=url,
            signature=signature,
            sha256=sha256,
            size_bytes=size_bytes,
            etag=etag,
            last_modified=last_modified,
            method=method,
            rows=rows,
            downloaded=True,
            removed=False,
            last_checked=now,
            last_downloaded=now,
            last_result="DOWNLOADED",
            error=None,
        )

    def mark_http_unchanged(self, source_id: str, artifact_id: str, *, url: str) -> dict[str, Any]:
        return self.mark_unchanged(source_id, artifact_id, url=url, reason="HTTP_304")

    def mark_error(self, source_id: str, artifact_id: str, *, url: str, error: str,
                   status_code: int | None = None, method: str | None = None) -> dict[str, Any]:
        return self._update(
            source_id, artifact_id,
            url=url,
            error=error[-2000:],
            http_status=status_code,
            method=method,
            last_checked=_now(),
            last_result="ERROR",
        )

    def mark_removed(self, source_id: str, artifact_id: str) -> dict[str, Any]:
        return self._update(
            source_id, artifact_id,
            removed=True,
            last_checked=_now(),
            last_result="REMOVED_UPSTREAM",
        )

    def source_rows(self, source_id: str) -> list[dict[str, Any]]:
        prefix = f"{source_id}::"
        return [dict(v) for k, v in self.data["resources"].items() if k.startswith(prefix) and isinstance(v, dict)]
