from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from .connectors.public_web import _is_upload_directory, _normalize_url
from .delivery import source_paths
from .streaming_snapshot import stream_response_snapshot

if TYPE_CHECKING:
    from .artifact_ledger import ArtifactLedger
    from .engine import IvoireDataEngine

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _expected_sha256(row: dict[str, Any]) -> str | None:
    for key in ("sha256", "upstream_signature"):
        value = str(row.get(key) or "").strip()
        if _SHA256.fullmatch(value):
            return value.lower()
    return None


def _artifact_url(row: dict[str, Any]) -> str:
    value = str(row.get("upstream_url") or "").strip()
    if value:
        return value
    artifact_id = str(row.get("artifact_id") or "")
    return artifact_id[4:] if artifact_id.startswith("url:") else ""


def proposed_action(engine: "IvoireDataEngine", row: dict[str, Any]) -> str:
    """Classify the safest repair path without performing network I/O."""
    source_id = str(row.get("source_id") or "")
    try:
        spec = engine.registry.get(source_id)
    except Exception:
        return "UNKNOWN_SOURCE"

    if spec.connector != "public_web":
        return "SOURCE_RESYNC"

    url = _artifact_url(row)
    if not url:
        return "SOURCE_RESYNC"
    normalized = _normalize_url(url)
    if normalized != url or _is_upload_directory(url):
        return "TOMBSTONE_INVALID_LEGACY_URL"

    status = str(row.get("status") or "").upper()
    if status not in {"LOCAL_MISSING", "CORRUPTED"}:
        return "SOURCE_RESYNC"

    expected = _expected_sha256(row)
    if not expected:
        # Without an expected digest, accepting bytes from a URL that disappeared from
        # discovery could silently replace historical content with a different object.
        return "SOURCE_RESYNC"

    source_host = (urlparse(spec.source_url).hostname or "").casefold()
    artifact_host = (urlparse(normalized).hostname or "").casefold()
    if not source_host or artifact_host != source_host:
        return "SOURCE_RESYNC"
    return "DIRECT_URL_REPAIR"


def annotate_plan(engine: "IvoireDataEngine", artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**row, "proposed_action": proposed_action(engine, row)} for row in artifacts]


def execute_direct_phase(
    engine: "IvoireDataEngine",
    ledger: "ArtifactLedger",
    artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Perform bounded repairs that do not require source rediscovery.

    Only public-web artifacts with a known SHA-256 and same-host URL are fetched
    directly. The downloaded bytes must match the historical digest before they are
    promoted to the local snapshot store. Other artifacts remain for the normal source
    re-sync phase.
    """
    import requests

    results: list[dict[str, Any]] = []
    for row in artifacts:
        source_id = str(row.get("source_id") or "")
        artifact_id = str(row.get("artifact_id") or "")
        action = proposed_action(engine, row)
        if action == "TOMBSTONE_INVALID_LEGACY_URL":
            try:
                engine.upstreams.mark_removed(source_id, artifact_id)
            finally:
                ledger.mark_removed(
                    source_id,
                    artifact_id,
                    reason="invalid legacy crawl URL (container URL or trailing encoded whitespace)",
                )
            results.append({
                "source_id": source_id,
                "artifact_id": artifact_id,
                "action": action,
                "status": "REMOVED",
            })
            continue

        if action != "DIRECT_URL_REPAIR":
            results.append({
                "source_id": source_id,
                "artifact_id": artifact_id,
                "action": action,
                "status": "DEFERRED",
            })
            continue

        spec = engine.registry.get(source_id)
        url = _normalize_url(_artifact_url(row))
        expected = _expected_sha256(row)
        assert expected is not None
        options = spec.options
        verify_ssl = bool(options.get("verify_ssl", True))
        max_bytes = int(options.get("max_bytes", 20_000_000))
        headers = {"User-Agent": engine.settings.user_agent}
        try:
            response = requests.get(
                url,
                timeout=120,
                headers=headers,
                stream=True,
                verify=verify_ssl,
            )
            response.raise_for_status()
            content_type = response.headers.get("content-type")
            snapshot = stream_response_snapshot(
                response,
                source_paths(engine.settings, spec)["documents"],
                source_id=source_id,
                url=url,
                content_type=content_type,
                name=Path(urlparse(url).path).name or None,
                expected_sha256=expected,
                max_bytes=max_bytes,
            )
            updated = engine.upstreams.mark_downloaded(
                source_id,
                artifact_id,
                url=url,
                signature=str(snapshot["sha256"]),
                sha256=str(snapshot["sha256"]),
                size_bytes=int(snapshot["size_bytes"]),
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                method="ARTIFACT_DIRECT_REPAIR",
                local_path=str(snapshot["local_path"]),
                extra={"content_type": content_type, "direct_repair": True},
            )
            ledger.ingest_upstream_row(updated)
            results.append({
                "source_id": source_id,
                "artifact_id": artifact_id,
                "action": action,
                "status": "REPAIRED",
                "sha256": snapshot["sha256"],
                "size_bytes": snapshot["size_bytes"],
                "local_path": snapshot["local_path"],
            })
        except Exception as exc:
            results.append({
                "source_id": source_id,
                "artifact_id": artifact_id,
                "action": action,
                "status": "FAILED_DIRECT_REPAIR",
                "error": str(exc)[-2000:],
            })
    return results
