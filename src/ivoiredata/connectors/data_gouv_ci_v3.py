from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, urljoin

from ..state_io import atomic_write_json
from ..streaming_snapshot import finalize_temp_snapshot, new_temp_path, stream_response_snapshot
from ..upstream_state import UpstreamState
from .data_gouv_ci_v2 import (
    API,
    PORTAL,
    _MaterializedRows,
    _archive_removed_tables,
    _cached_materialized,
    _classification,
    _dataset_id,
    _discover_official,
    _full_download_streaming,
    _has_physical_cache,
    _legacy_signature,
    _lines_download_streaming,
    _safe_table,
    _signature,
    _snapshot_root,
)


def _detail_url(dsid: str) -> str:
    return f"{API}/datasets/{quote(dsid, safe='')}"


def _attachment_entries(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize Data Fair dataset-level attachment metadata.

    Data Fair exposes dataset attachments in the dataset metadata. Different server
    versions have represented the collection as a list, a keyed object or an object
    containing ``items``/``files``; normalize all documented/common shapes without
    guessing attachment URLs from portal HTML.
    """
    raw = detail.get("attachments")
    if isinstance(raw, list):
        out: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                out.append(dict(item))
            elif isinstance(item, str) and item.strip():
                out.append({"name": item.strip()})
        return out
    if isinstance(raw, dict):
        for key in ("items", "files", "results", "data"):
            value = raw.get(key)
            if isinstance(value, list):
                return _attachment_entries({"attachments": value})
        out = []
        for key, value in raw.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("name", str(key))
                out.append(item)
            elif isinstance(value, str):
                out.append({"name": str(key), "value": value})
        return out
    return []


def _attachment_name(entry: dict[str, Any]) -> str | None:
    for key in ("name", "filename", "fileName", "path", "title"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\\", "/").rsplit("/", 1)[-1]
    return None


def _attachment_url(dsid: str, entry: dict[str, Any], *, detail_url: str) -> str | None:
    for key in ("downloadUrl", "downloadURL", "url", "href"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return urljoin(detail_url, value.strip())
    name = _attachment_name(entry)
    if not name:
        return None
    # Official Data Fair route for dataset-level metadata attachments.
    return f"{API}/datasets/{quote(dsid, safe='')}/metadata-attachments/{quote(name, safe='')}"


def _attachment_signature(entry: dict[str, Any], url: str) -> str:
    payload = {"url": url, "metadata": entry}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _attachment_artifact_id(dsid: str, name: str) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
    return f"attachment:{dsid}:{digest}"


@dataclass
class _AttachmentMaterialized:
    path: Path
    snapshot: dict[str, Any]
    source_url: str
    method: str
    row_format: str
    rows_payload: list[dict[str, Any]]
    row_count_hint: int | None = None

    def rows(self) -> Iterator[dict[str, Any]]:
        yield from (dict(item) for item in self.rows_payload)


def _load_attachment_manifest(path: Path) -> list[dict[str, Any]] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = payload.get("attachments") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    normalized = [dict(row) for row in rows if isinstance(row, dict)]
    if len(normalized) != len(rows):
        return None
    for row in normalized:
        local = str(row.get("local_path") or "").strip()
        if not local or not Path(local).is_file():
            return None
    return normalized


def _cached_materialized_v3(
    upstream: UpstreamState,
    source_id: str,
    artifact: str,
    signature: str,
) -> tuple[_MaterializedRows | _AttachmentMaterialized, dict[str, Any]] | None:
    state = upstream.get(source_id, artifact)
    if str(state.get("method") or "").upper() != "ATTACHMENTS_STREAM":
        return _cached_materialized(upstream, source_id, artifact, signature)
    path = upstream.cached_path(source_id, artifact, signature)
    if path is None:
        return None
    rows = _load_attachment_manifest(path)
    if rows is None:
        return None
    return (
        _AttachmentMaterialized(
            path=path,
            snapshot={
                "sha256": state.get("sha256"),
                "size_bytes": state.get("size_bytes"),
                "local_path": str(path),
            },
            source_url=str(state.get("url") or f"{PORTAL}/datasets/{artifact.removeprefix('dataset:')}"),
            method="ATTACHMENTS_STREAM",
            row_format="attachment-manifest",
            rows_payload=rows,
            row_count_hint=len(rows),
        ),
        state,
    )


def _has_physical_cache_v3(
    upstream: UpstreamState | None,
    source_id: str,
    artifact: str,
    signature: str,
) -> bool:
    if upstream is None:
        return False
    state = upstream.get(source_id, artifact)
    if str(state.get("method") or "").upper() != "ATTACHMENTS_STREAM":
        return _has_physical_cache(upstream, source_id, artifact, signature)
    return _cached_materialized_v3(upstream, source_id, artifact, signature) is not None


def _write_attachment_manifest(
    raw_root: Path,
    *,
    source_id: str,
    dsid: str,
    detail_url: str,
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "dataset_id": dsid,
        "source_url": detail_url,
        "attachment_count": len(attachments),
        "attachments": sorted(attachments, key=lambda item: str(item.get("name") or "")),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    temp = new_temp_path(raw_root, prefix=f"{dsid}-attachments-manifest")
    try:
        with temp.open("wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        return finalize_temp_snapshot(
            raw_root,
            temp_path=temp,
            source_id=source_id,
            url=detail_url,
            content_type="application/json",
            name=f"{dsid}-attachments-manifest.json",
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
        )
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _download_metadata_attachments(
    session,
    *,
    source_id: str,
    dsid: str,
    detail_url: str,
    detail: dict[str, Any],
    raw_root: Path,
    upstream: UpstreamState | None,
) -> tuple[_AttachmentMaterialized | None, dict[str, int]]:
    entries = _attachment_entries(detail)
    stats = {"attachment_files": 0, "attachments_downloaded": 0, "attachments_reused": 0}
    if not entries:
        return None, stats

    rows: list[dict[str, Any]] = []
    active_child_ids: set[str] = set()
    for entry in entries:
        name = _attachment_name(entry)
        url = _attachment_url(dsid, entry, detail_url=detail_url)
        if not name or not url:
            continue
        child_id = _attachment_artifact_id(dsid, name)
        active_child_ids.add(child_id)
        signature = _attachment_signature(entry, url)
        cached_path = upstream.cached_path(source_id, child_id, signature) if upstream else None
        child_state = upstream.get(source_id, child_id) if upstream else {}

        if cached_path is not None:
            local_path = str(cached_path)
            sha256 = str(child_state.get("sha256") or "") or None
            size_bytes = int(child_state.get("size_bytes") or cached_path.stat().st_size)
            content_type = child_state.get("content_type")
            stats["attachments_reused"] += 1
        else:
            response = session.get(
                url,
                timeout=300,
                stream=True,
                headers={"Accept": "*/*"},
            )
            response.raise_for_status()
            content_type = response.headers.get("content-type")
            snapshot = stream_response_snapshot(
                response,
                raw_root,
                source_id=source_id,
                url=response.url,
                content_type=content_type,
                name=f"{dsid}-{name}",
            )
            local_path = str(snapshot["local_path"])
            sha256 = str(snapshot["sha256"])
            size_bytes = int(snapshot["size_bytes"])
            stats["attachments_downloaded"] += 1
            if upstream:
                upstream.mark_downloaded(
                    source_id,
                    child_id,
                    url=response.url,
                    signature=signature,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                    method="DATAFAIR_METADATA_ATTACHMENT",
                    rows=None,
                    local_path=local_path,
                    extra={
                        "artifact_type": "dataset_attachment",
                        "parent_dataset_id": dsid,
                        "attachment_name": name,
                        "content_type": content_type,
                    },
                )

        rows.append({
            "attachment_name": name,
            "attachment_url": url,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "local_path": local_path,
            "metadata_json": json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str),
        })

    if not rows:
        return None, stats

    if upstream:
        for row in upstream.source_rows(source_id):
            if str(row.get("parent_dataset_id") or "") != dsid:
                continue
            child_id = str(row.get("artifact_id") or "")
            if child_id.startswith("attachment:") and child_id not in active_child_ids and not row.get("removed"):
                upstream.mark_removed(source_id, child_id)

    manifest = _write_attachment_manifest(
        raw_root,
        source_id=source_id,
        dsid=dsid,
        detail_url=detail_url,
        attachments=rows,
    )
    stats["attachment_files"] = len(rows)
    return (
        _AttachmentMaterialized(
            path=Path(str(manifest["local_path"])),
            snapshot=dict(manifest),
            source_url=detail_url,
            method="ATTACHMENTS_STREAM",
            row_format="attachment-manifest",
            rows_payload=rows,
            row_count_hint=len(rows),
        ),
        stats,
    )


def data_gouv_ci_resource_v3(
    *,
    source_id: str = "civ_datagouv_catalog",
    dataset_ids: list[str] | None = None,
    limit: int | None = None,
    force: bool = False,
    user_agent: str = "IvoireData/0.8.4",
    snapshot_dir: Path | None = None,
    metadata_base: dict[str, Any] | None = None,
    upstream_state_path: Path | None = None,
):
    """Data.gouv CI collector with physical streaming, ghosts and attachments.

    Retrieval order is authoritative and conservative: Data Fair ``/full`` first,
    official ``/lines`` fallback second, then dataset-level metadata attachments when
    the detail endpoint exists. A catalogue entry becomes ``UPSTREAM_GHOST`` only when
    /full, /lines and the authoritative detail endpoint all return 404.
    """
    import dlt
    import requests

    base = dict(metadata_base or {})

    @dlt.resource(name="data_gouv_ci", write_disposition="replace")
    def resource():
        session = requests.Session()
        session.headers.update({"User-Agent": user_agent})
        loaded_signatures = dlt.current.resource_state().setdefault("dataset_signatures", {})
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None
        raw_root = _snapshot_root(snapshot_dir)
        catalog = _discover_official(session)
        wanted = {str(value) for value in (dataset_ids or []) if value}
        selected: list[dict[str, Any]] = []
        catalog_ids: set[str] = set()
        classifications: dict[str, dict[str, Any]] = {}

        for meta in catalog:
            dsid = _dataset_id(meta)
            if not dsid:
                continue
            catalog_ids.add(dsid)
            identifiers = {dsid, str(meta.get("slug") or "")}
            if not wanted or identifiers & wanted:
                selected.append(meta)
            classified = _classification(meta, dsid, base)
            classifications[dsid] = classified
            row = dict(meta)
            row["__ivoiredata_source_url"] = f"{PORTAL}/datasets/{dsid}"
            row["__ivoiredata_dataset_signature"] = _signature(meta)
            row.update(classified)
            yield dlt.mark.with_table_name(row, "datagouv_catalog")

        if limit is not None:
            selected = selected[: max(0, int(limit))]

        stats: dict[str, Any] = {
            "catalog_visible_anonymous": len(catalog_ids),
            "selected": len(selected),
            "unchanged": 0,
            "backfill_missing_raw": 0,
            "physically_backfilled": 0,
            "reappeared": 0,
            "replayed_from_local_cache": 0,
            "downloaded": 0,
            "via_full_stream": 0,
            "via_lines_stream": 0,
            "via_attachments_stream": 0,
            "attachment_files": 0,
            "attachments_downloaded": 0,
            "attachments_reused": 0,
            "empty": 0,
            "failed": 0,
            "upstream_ghost": 0,
            "ghost_unchanged": 0,
            "ghost_rechecked": 0,
            "upstream_ghost_ids": [],
            "removed_upstream": 0,
            "removed_upstream_ids": [],
            "archived_removed_tables": [],
            "business_rows_changed": 0,
            "failures": [],
            "pagination": "Data Fair page>=1 until advertised count is reached; /lines follows next cursor until absent",
            "streaming": True,
            "attachment_semantics": "dataset-level Data Fair metadata attachments are streamed raw and represented by a digest-addressed manifest; archives are not unpacked",
            "force_semantics": "force checks current catalogue signatures; unchanged physical datasets and unchanged confirmed ghosts do not redownload/reprobe bodies",
            "physical_truth": "DLT signature alone never proves a fetched artifact; missing raw snapshots are backfilled",
            "ghost_semantics": "catalogue-visible entry is UPSTREAM_GHOST only after /full=404, /lines=404 and Data Fair detail=404",
        }

        if upstream:
            previous_rows = upstream.source_rows(source_id)
            previous = {
                str(row.get("artifact_id", ""))[8:]
                for row in previous_rows
                if str(row.get("artifact_id", "")).startswith("dataset:") and not row.get("removed")
            }
            removed_ids = sorted(previous - catalog_ids)
            for removed in removed_ids:
                upstream.mark_removed(source_id, f"dataset:{removed}")
            stats["removed_upstream"] = len(removed_ids)
            stats["removed_upstream_ids"] = removed_ids
            stats["archived_removed_tables"] = _archive_removed_tables(snapshot_dir, removed_ids)

        for meta in selected:
            dsid = _dataset_id(meta)
            assert dsid is not None
            signature = _signature(meta)
            legacy_signature = _legacy_signature(meta)
            artifact = f"dataset:{dsid}"
            cached_state = upstream.get(source_id, artifact) if upstream else {}
            cached_result = str(cached_state.get("last_result") or "").upper()
            if cached_state.get("removed"):
                loaded_signatures.pop(dsid, None)
                stats["reappeared"] += 1
            if cached_result == "UPSTREAM_GHOST" and cached_state.get("signature") == signature:
                stats["upstream_ghost"] += 1
                stats["ghost_unchanged"] += 1
                stats["upstream_ghost_ids"].append(dsid)
                continue
            if cached_result == "UPSTREAM_GHOST":
                stats["ghost_rechecked"] += 1
            loaded = loaded_signatures.get(dsid)

            physical_current = _has_physical_cache_v3(upstream, source_id, artifact, signature)
            if loaded == signature and physical_current:
                stats["unchanged"] += 1
                assert upstream is not None
                upstream.mark_unchanged(
                    source_id,
                    artifact,
                    signature=signature,
                    url=f"{PORTAL}/datasets/{dsid}",
                    reason="SIGNATURE_AND_LOCAL_SNAPSHOT",
                )
                continue

            historical_without_raw = loaded in {signature, legacy_signature} and not physical_current
            if historical_without_raw:
                stats["backfill_missing_raw"] += 1

            materialized: _MaterializedRows | _AttachmentMaterialized | None = None
            replay = _cached_materialized_v3(upstream, source_id, artifact, signature) if upstream else None
            if replay is not None:
                materialized, _ = replay
                stats["replayed_from_local_cache"] += 1
            else:
                full_error: Exception | None = None
                full_status: int | None = None
                lines_error: Exception | None = None
                lines_status: int | None = None
                try:
                    materialized = _full_download_streaming(
                        session,
                        dsid,
                        snapshot_dir=raw_root,
                        source_id=source_id,
                    )
                    stats["via_full_stream"] += 1
                except Exception as exc:
                    full_error = exc
                    full_status = getattr(getattr(exc, "response", None), "status_code", None)
                    try:
                        materialized = _lines_download_streaming(
                            session,
                            dsid,
                            snapshot_dir=raw_root,
                            source_id=source_id,
                        )
                        stats["via_lines_stream"] += 1
                    except Exception as exc2:
                        lines_error = exc2
                        lines_status = getattr(getattr(exc2, "response", None), "status_code", None)

                        detail_url = _detail_url(dsid)
                        detail_status: int | None = None
                        detail_payload: dict[str, Any] | None = None
                        detail_error: Exception | None = None
                        try:
                            detail_response = session.get(
                                detail_url,
                                timeout=60,
                                headers={"Accept": "application/json"},
                            )
                            detail_status = int(detail_response.status_code)
                            if detail_response.ok:
                                value = detail_response.json()
                                detail_payload = value if isinstance(value, dict) else {}
                        except Exception as exc3:
                            detail_error = exc3

                        if full_status == 404 and lines_status == 404 and detail_status == 404:
                            evidence = {
                                "catalog_visible": True,
                                "full_status": 404,
                                "lines_status": 404,
                                "detail_status": 404,
                                "detail_url": detail_url,
                                "classification": "CATALOG_VISIBLE_BUT_AUTHORITY_DATASET_MISSING",
                            }
                            stats["upstream_ghost"] += 1
                            stats["upstream_ghost_ids"].append(dsid)
                            if upstream:
                                upstream.mark_ghost(
                                    source_id,
                                    artifact,
                                    url=detail_url,
                                    signature=signature,
                                    evidence=evidence,
                                )
                            continue

                        attachment_error: Exception | None = None
                        if detail_status == 200 and detail_payload is not None:
                            try:
                                attachment_materialized, attachment_stats = _download_metadata_attachments(
                                    session,
                                    source_id=source_id,
                                    dsid=dsid,
                                    detail_url=detail_url,
                                    detail=detail_payload,
                                    raw_root=raw_root,
                                    upstream=upstream,
                                )
                                if attachment_materialized is not None:
                                    materialized = attachment_materialized
                                    stats["via_attachments_stream"] += 1
                                    for key, value in attachment_stats.items():
                                        stats[key] += int(value)
                            except Exception as exc4:
                                attachment_error = exc4

                        if materialized is None:
                            error = (
                                f"/full: {full_error}; /lines: {lines_error}; "
                                f"detail_status={detail_status}; detail_error={detail_error}; "
                                f"attachments_error={attachment_error or 'none/absent'}"
                            )
                            stats["failed"] += 1
                            stats["failures"].append({
                                "dataset_id": dsid,
                                "full_status": full_status,
                                "lines_status": lines_status,
                                "detail_status": detail_status,
                                "error": error[:1000],
                            })
                            if upstream:
                                upstream.mark_error(
                                    source_id,
                                    artifact,
                                    url=f"{API}/datasets/{quote(dsid, safe='')}/full",
                                    error=error,
                                    status_code=lines_status,
                                    method="FULL_STREAM+LINES_STREAM+ATTACHMENTS_STREAM",
                                )
                            continue

                stats["downloaded"] += 1
                if historical_without_raw:
                    stats["physically_backfilled"] += 1

            assert materialized is not None
            snapshot = materialized.snapshot
            raw_sha = str(snapshot.get("sha256") or "") or None
            raw_path = str(snapshot.get("local_path") or "") or None
            raw_size = int(snapshot.get("size_bytes") or 0)
            if upstream:
                upstream.mark_downloaded(
                    source_id,
                    artifact,
                    url=materialized.source_url,
                    signature=signature,
                    sha256=raw_sha,
                    size_bytes=raw_size,
                    method=materialized.method,
                    rows=materialized.row_count_hint,
                    local_path=raw_path,
                    extra={
                        "streaming": True,
                        "body_changed": replay is None,
                        "attachment_backed": materialized.method == "ATTACHMENTS_STREAM",
                    },
                )

            classified = classifications.get(dsid) or _classification(meta, dsid, base)
            table = _safe_table(dsid)
            row_count = 0
            for index, source_row in enumerate(materialized.rows()):
                row_count += 1
                item = dict(source_row)
                item.update({
                    "__ivoiredata_dataset_id": dsid,
                    "__ivoiredata_source_url": f"{PORTAL}/datasets/{dsid}",
                    "__ivoiredata_row_index": index,
                    "__ivoiredata_raw_sha256": raw_sha,
                    "__ivoiredata_raw_path": raw_path,
                    "__ivoiredata_fetch_method": materialized.method,
                    **classified,
                })
                yield dlt.mark.with_table_name(item, table)

            if row_count == 0:
                stats["empty"] += 1
            loaded_signatures[dsid] = signature
            stats["business_rows_changed"] += row_count
            if upstream:
                upstream.mark_downloaded(
                    source_id,
                    artifact,
                    url=materialized.source_url,
                    signature=signature,
                    sha256=raw_sha,
                    size_bytes=raw_size,
                    method=materialized.method,
                    rows=row_count,
                    local_path=raw_path,
                    extra={
                        "streaming": True,
                        "body_changed": replay is None,
                        "attachment_backed": materialized.method == "ATTACHMENTS_STREAM",
                    },
                )

        if snapshot_dir:
            atomic_write_json(snapshot_dir / "datagouv_sync_stats.json", stats)
        yield dlt.mark.with_table_name(
            {"run_stats_json": json.dumps(stats, ensure_ascii=False), **stats},
            "datagouv_sync_stats",
        )

    return resource()


data_gouv_ci_resource = data_gouv_ci_resource_v3

__all__ = [
    "data_gouv_ci_resource",
    "data_gouv_ci_resource_v3",
    "_attachment_entries",
    "_attachment_name",
    "_attachment_url",
    "_download_metadata_attachments",
]
