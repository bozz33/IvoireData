from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .locks import file_lock
from .state_io import atomic_write_json, load_json


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class UpstreamState:
    """Persistent cache metadata used to avoid transferring unchanged upstream data.

    API, scheduler and one-shot containers share this file. Every mutation therefore
    locks, reloads the newest disk state, applies one change and atomically replaces the
    file. This prevents two concurrent sources from losing each other's cache records.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        payload = load_json(self.path, {"schema_version": 1, "resources": {}})
        if not isinstance(payload, dict):
            payload = {"schema_version": 1, "resources": {}}
        payload.setdefault("schema_version", 1)
        resources = payload.setdefault("resources", {})
        if not isinstance(resources, dict):
            payload["resources"] = {}
        return payload

    def _refresh(self) -> None:
        self.data = self._load()

    @staticmethod
    def key(source_id: str, artifact_id: str) -> str:
        return f"{source_id}::{artifact_id}"

    def get(self, source_id: str, artifact_id: str) -> dict[str, Any]:
        self._refresh()
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

    def cached_path(self, source_id: str, artifact_id: str, signature: str | None = None) -> Path | None:
        row = self.get(source_id, artifact_id)
        if signature is not None and row.get("signature") != signature:
            return None
        value = row.get("local_path")
        if not value:
            return None
        path = Path(str(value))
        return path if path.exists() else None

    def _update(self, source_id: str, artifact_id: str, **values: Any) -> dict[str, Any]:
        with file_lock(self.lock_path, timeout=60):
            self.data = self._load()
            key = self.key(source_id, artifact_id)
            row = self.data["resources"].setdefault(key, {})
            if not isinstance(row, dict):
                row = {}
                self.data["resources"][key] = row
            row.update(values)
            row["source_id"] = source_id
            row["artifact_id"] = artifact_id
            atomic_write_json(self.path, self.data)
            return dict(row)

    def mark_unchanged(self, source_id: str, artifact_id: str, *, signature: str | None = None,
                       url: str | None = None, reason: str = "signature", etag: str | None = None,
                       last_modified: str | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        values: dict[str, Any] = {
            "last_checked": _now(),
            "last_result": "UNCHANGED",
            "unchanged_reason": reason,
            # UNCHANGED is evidence that a previous version is already materialized or
            # cached locally. This also lets migration-adopted datasets participate in
            # future removed-upstream reconciliation.
            "downloaded": True,
            "removed": False,
            "error": None,
            "http_status": None,
        }
        if signature is not None:
            values["signature"] = signature
        if url is not None:
            values["url"] = url
        if etag is not None:
            values["etag"] = etag
        if last_modified is not None:
            values["last_modified"] = last_modified
        if extra:
            values.update(extra)
        return self._update(source_id, artifact_id, **values)

    def mark_downloaded(self, source_id: str, artifact_id: str, *, url: str, signature: str | None,
                        sha256: str | None, size_bytes: int | None, etag: str | None = None,
                        last_modified: str | None = None, method: str | None = None,
                        rows: int | None = None, local_path: str | None = None,
                        extra: dict[str, Any] | None = None) -> dict[str, Any]:
        now = _now()
        values: dict[str, Any] = dict(
            url=url,
            signature=signature,
            sha256=sha256,
            size_bytes=size_bytes,
            etag=etag,
            last_modified=last_modified,
            method=method,
            rows=rows,
            local_path=local_path,
            downloaded=True,
            removed=False,
            last_checked=now,
            last_downloaded=now,
            last_result="DOWNLOADED",
            error=None,
            http_status=None,
        )
        if extra:
            values.update(extra)
        return self._update(source_id, artifact_id, **values)

    def mark_http_unchanged(self, source_id: str, artifact_id: str, *, url: str,
                            extra: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.mark_unchanged(source_id, artifact_id, url=url, reason="HTTP_304", extra=extra)

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
            error=None,
            http_status=None,
            last_checked=_now(),
            last_result="REMOVED_UPSTREAM",
        )

    def source_rows(self, source_id: str) -> list[dict[str, Any]]:
        self._refresh()
        prefix = f"{source_id}::"
        return [dict(v) for k, v in self.data["resources"].items() if k.startswith(prefix) and isinstance(v, dict)]
