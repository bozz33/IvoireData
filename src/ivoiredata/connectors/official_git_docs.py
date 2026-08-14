from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlparse

from ..cleaning import clean_text
from ..metadata import classify_from_base, title_from_text
from ..snapshots import save_snapshot
from ..state_io import atomic_write_json
from ..upstream_state import UpstreamState
from .public_web import chunk_text

_TEXT_EXTENSIONS = {".md", ".mdx", ".markdown", ".rst", ".adoc", ".asciidoc", ".txt"}


def parse_github_tree_url(url: str) -> tuple[str, str] | None:
    """Return (owner/repo, ref) for a canonical GitHub tree URL."""
    parsed = urlparse(url)
    if (parsed.hostname or "").casefold() not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[2] != "tree":
        return None
    return f"{parts[0]}/{parts[1]}", parts[3]


def _path_allowed(path: str, prefixes: list[str], excludes: list[re.Pattern[str]]) -> bool:
    if Path(path).suffix.casefold() not in _TEXT_EXTENSIONS:
        return False
    if prefixes and not any(path.startswith(prefix) for prefix in prefixes):
        return False
    return not any(pattern.search(path) for pattern in excludes)


def _api_headers(user_agent: str) -> dict[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_json(session, url: str, headers: dict[str, str]) -> dict[str, Any]:
    response = session.get(url, headers=headers, timeout=120)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected GitHub response at {url}")
    return payload


def _extract_text(raw: bytes) -> str:
    return clean_text(raw.decode("utf-8", "replace"))


def official_git_docs_resource(
    *,
    source_id: str,
    repository: str,
    ref: str,
    user_agent: str = "IvoireData/0.8.3",
    include_prefixes: Iterable[str] = (),
    exclude_patterns: Iterable[str] = (),
    max_pages: int = 100_000,
    max_bytes_per_page: int = 12_000_000,
    max_new_bytes_per_run: int = 500_000_000,
    request_pause_seconds: float = 0.0,
    snapshot_dir: Path | None = None,
    metadata_base: dict[str, Any] | None = None,
    upstream_state_path: Path | None = None,
    license_name: str | None = None,
    license_url: str | None = None,
    training_eligible: bool = False,
    license_review_status: str = "UNREVIEWED",
):
    """Materialize official documentation from a GitHub repository incrementally.

    Only branch metadata and, when the commit changed, the recursive Git tree are
    requested on every run. Document bodies are fetched only when their Git blob SHA is
    new or changed. A stable commit therefore performs zero document-body downloads.
    """
    import dlt
    import requests
    import time

    max_pages = max(1, int(max_pages))
    max_bytes_per_page = max(100_000, int(max_bytes_per_page))
    max_new_bytes_per_run = max(0, int(max_new_bytes_per_run))
    pause = max(0.0, min(float(request_pause_seconds), 2.0))
    prefixes = [str(value) for value in include_prefixes if str(value)]
    excludes = [re.compile(str(value), re.I) for value in exclude_patterns if str(value)]
    base = dict(metadata_base or {})

    @dlt.resource(name="official_docs", write_disposition="merge", primary_key="record_id")
    def resource():
        session = requests.Session()
        headers = _api_headers(user_agent)
        upstream = UpstreamState(upstream_state_path) if upstream_state_path else None
        state = dlt.current.resource_state()
        materialized = state.setdefault("git_materialized_blobs_v1", {})
        content_hashes = state.setdefault("git_content_hashes_v1", {})
        chunk_counts = state.setdefault("git_chunk_counts_v1", {})
        known_paths = set(str(value) for value in state.get("git_known_paths_v1", []) if value)
        last_complete_commit = str(state.get("git_last_complete_commit_v1") or "")

        owner_repo = repository.strip().strip("/")
        if owner_repo.count("/") != 1:
            raise ValueError(f"invalid GitHub repository: {repository}")
        encoded_ref = quote(ref, safe="")
        branch_url = f"https://api.github.com/repos/{owner_repo}/branches/{encoded_ref}"
        branch = _get_json(session, branch_url, headers)
        commit = str(((branch.get("commit") or {}).get("sha")) or "")
        tree_sha = str((((branch.get("commit") or {}).get("commit") or {}).get("tree") or {}).get("sha") or "")
        if not commit or not tree_sha:
            raise RuntimeError(f"cannot resolve GitHub commit/tree for {owner_repo}@{ref}")

        stats: dict[str, Any] = {
            "source_id": source_id,
            "root_url": f"https://github.com/{owner_repo}/tree/{ref}",
            "final_root_url": f"https://github.com/{owner_repo}/tree/{ref}",
            "source_strategy": "OFFICIAL_GIT",
            "git_repository": owner_repo,
            "git_ref": ref,
            "git_commit": commit,
            "git_tree": tree_sha,
            "discovery_methods": ["git_tree"],
            "authoritative_sitemap": False,
            "discovery_complete": True,
            "discovery_truncated": False,
            "discovered_pages": 0,
            "selected_pages": 0,
            "downloaded": 0,
            "downloaded_bytes": 0,
            "replayed_from_local_cache": 0,
            "unchanged_git": 0,
            "unchanged_lastmod": 0,
            "unchanged_http304": 0,
            "unchanged_sha256": 0,
            "content_unchanged": 0,
            "new_documents": 0,
            "modified_documents": 0,
            "removed_upstream": 0,
            "failed": 0,
            "deferred_budget": 0,
            "skipped_oversize": 0,
            "backlog_count": 0,
            "business_chunks": 0,
            "chunks_created": 0,
            "chunks_reused": 0,
            "body_requests_avoided": 0,
            "incremental_efficiency": 0.0,
            "failures": [],
            "license_name": license_name,
            "license_url": license_url,
            "license_review_status": license_review_status,
            "training_eligible": bool(training_eligible),
        }

        # One small branch request is enough when the canonical documentation commit is
        # unchanged. No recursive tree and no document-body request is made.
        if last_complete_commit == commit and materialized:
            stats["discovered_pages"] = len(materialized)
            stats["selected_pages"] = len(materialized)
            stats["unchanged_git"] = len(materialized)
            stats["body_requests_avoided"] = len(materialized)
            stats["chunks_reused"] = sum(int(value or 0) for value in chunk_counts.values())
            stats["business_chunks"] = stats["chunks_reused"]
            stats["incremental_efficiency"] = 100.0
            for path, blob_sha in materialized.items():
                if upstream:
                    upstream.mark_unchanged(
                        source_id,
                        f"git:{path}",
                        signature=str(blob_sha),
                        url=f"https://github.com/{owner_repo}/blob/{commit}/{path}",
                        reason="GIT_COMMIT_UNCHANGED",
                        extra={"git_commit": commit, "git_ref": ref, "git_path": path},
                    )
            if snapshot_dir:
                atomic_write_json(snapshot_dir / "official_docs_sync_stats.json", stats)
            rid = hashlib.sha256(f"{source_id}|official-docs-stats".encode()).hexdigest()
            yield dlt.mark.with_table_name({"record_id": rid, "run_stats_json": json.dumps(stats, ensure_ascii=False), **stats}, "official_docs_sync_stats")
            return

        tree_url = f"https://api.github.com/repos/{owner_repo}/git/trees/{tree_sha}?recursive=1"
        tree = _get_json(session, tree_url, headers)
        if tree.get("truncated"):
            stats["discovery_complete"] = False
            stats["discovery_truncated"] = True

        entries: list[dict[str, Any]] = []
        for item in tree.get("tree", []):
            if not isinstance(item, dict) or item.get("type") != "blob":
                continue
            path = str(item.get("path") or "")
            if not path or not _path_allowed(path, prefixes, excludes):
                continue
            entries.append({"path": path, "sha": str(item.get("sha") or ""), "size": int(item.get("size") or 0)})
        entries.sort(key=lambda item: item["path"])
        stats["discovered_pages"] = len(entries)
        if len(entries) > max_pages:
            stats["discovery_complete"] = False
            stats["discovery_truncated"] = True
            entries = entries[:max_pages]
        stats["selected_pages"] = len(entries)
        current_paths = {item["path"] for item in entries}

        if stats["discovery_complete"]:
            removed = sorted(known_paths - current_paths)
            stats["removed_upstream"] = len(removed)
            for path in removed:
                page_url = f"https://github.com/{owner_repo}/blob/{commit}/{path}"
                page_id = hashlib.sha256(f"{source_id}|page|{path}".encode()).hexdigest()
                yield dlt.mark.with_table_name(
                    {
                        "record_id": page_id,
                        "source_id": source_id,
                        "page_url": page_url,
                        "git_repository": owner_repo,
                        "git_ref": ref,
                        "git_commit": commit,
                        "git_path": path,
                        "active": False,
                        "content_sha256": content_hashes.get(path),
                    },
                    "official_docs_pages",
                )
                materialized.pop(path, None)
                content_hashes.pop(path, None)
                chunk_counts.pop(path, None)
                if upstream:
                    upstream.mark_removed(source_id, f"git:{path}")

        budget = max_new_bytes_per_run
        for item in entries:
            path = item["path"]
            blob_sha = item["sha"]
            size = int(item["size"] or 0)
            previous_blob = str(materialized.get(path) or "")
            if previous_blob == blob_sha:
                stats["unchanged_git"] += 1
                stats["body_requests_avoided"] += 1
                stats["chunks_reused"] += int(chunk_counts.get(path) or 0)
                if upstream:
                    upstream.mark_unchanged(
                        source_id,
                        f"git:{path}",
                        signature=blob_sha,
                        url=f"https://github.com/{owner_repo}/blob/{commit}/{path}",
                        reason="GIT_BLOB_SHA",
                        extra={"git_commit": commit, "git_ref": ref, "git_path": path},
                    )
                continue
            if size > max_bytes_per_page:
                stats["skipped_oversize"] += 1
                stats["failures"].append({"path": path, "error": "PAGE_TOO_LARGE", "size": size, "limit": max_bytes_per_page})
                continue
            if size > budget:
                stats["deferred_budget"] += 1
                continue

            artifact = f"git:{path}"
            cached = upstream.cached_path(source_id, artifact, signature=blob_sha) if upstream else None
            local: Path | None = None
            replayed = False
            try:
                if cached is not None:
                    raw = cached.read_bytes()
                    local = cached
                    replayed = True
                    stats["replayed_from_local_cache"] += 1
                else:
                    raw_url = f"https://raw.githubusercontent.com/{owner_repo}/{commit}/{quote(path, safe='/')}"
                    response = session.get(raw_url, headers={"User-Agent": user_agent}, timeout=120)
                    response.raise_for_status()
                    raw = response.content
                    if len(raw) > max_bytes_per_page:
                        stats["skipped_oversize"] += 1
                        stats["failures"].append({"path": path, "error": "PAGE_TOO_LARGE", "size": len(raw), "limit": max_bytes_per_page})
                        continue
                    budget = max(0, budget - len(raw))
                    stats["downloaded"] += 1
                    stats["downloaded_bytes"] += len(raw)
                    snap = save_snapshot(
                        snapshot_dir,
                        source_id=source_id,
                        url=raw_url,
                        content=raw,
                        content_type="text/plain; charset=utf-8",
                        name=f"git-{hashlib.sha256(path.encode()).hexdigest()[:16]}",
                    )
                    local = Path(str(snap["local_path"])) if snap.get("local_path") else None
                    raw_sha = hashlib.sha256(raw).hexdigest()
                    if upstream:
                        upstream.mark_downloaded(
                            source_id,
                            artifact,
                            url=raw_url,
                            signature=blob_sha,
                            sha256=raw_sha,
                            size_bytes=len(raw),
                            method="OFFICIAL_GIT_BLOB",
                            local_path=str(local) if local else None,
                            extra={"git_commit": commit, "git_ref": ref, "git_path": path, "git_blob_sha": blob_sha},
                        )

                text = _extract_text(raw)
                if not text:
                    stats["failed"] += 1
                    stats["failures"].append({"path": path, "error": "NO_EXTRACTABLE_TEXT"})
                    continue
                raw_sha = hashlib.sha256(raw).hexdigest()
                content_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                page_url = f"https://github.com/{owner_repo}/blob/{commit}/{path}"
                classified = classify_from_base(base, page_url, text, document_type="DEVELOPER_DOCUMENTATION")
                title = title_from_text(text)
                page_id = hashlib.sha256(f"{source_id}|page|{path}".encode()).hexdigest()
                previous_content = str(content_hashes.get(path) or "")
                yield dlt.mark.with_table_name(
                    {
                        "record_id": page_id,
                        "source_id": source_id,
                        "page_url": page_url,
                        "document_title": title,
                        "active": True,
                        "source_strategy": "OFFICIAL_GIT",
                        "git_repository": owner_repo,
                        "git_ref": ref,
                        "git_commit": commit,
                        "git_tree": tree_sha,
                        "git_path": path,
                        "git_blob_sha": blob_sha,
                        "raw_sha256": raw_sha,
                        "content_sha256": content_sha,
                        "content_type": "text/plain; charset=utf-8",
                        "local_snapshot": str(local) if local else None,
                        **classified,
                    },
                    "official_docs_pages",
                )

                if previous_content == content_sha and previous_content:
                    stats["content_unchanged"] += 1
                    stats["chunks_reused"] += int(chunk_counts.get(path) or 0)
                else:
                    emitted = 0
                    for index, chunk in enumerate(chunk_text(text, size=5000, overlap=300)):
                        chunk_id = hashlib.sha256(f"{source_id}|{path}|{content_sha}|{index}".encode()).hexdigest()
                        emitted += 1
                        yield dlt.mark.with_table_name(
                            {
                                "record_id": chunk_id,
                                "chunk_id": chunk_id,
                                "page_record_id": page_id,
                                "source_id": source_id,
                                "source_url": page_url,
                                "page_url": page_url,
                                "document_title": title,
                                "source_strategy": "OFFICIAL_GIT",
                                "git_repository": owner_repo,
                                "git_ref": ref,
                                "git_commit": commit,
                                "git_path": path,
                                "git_blob_sha": blob_sha,
                                "raw_sha256": raw_sha,
                                "content_sha256": content_sha,
                                "chunk_index": index,
                                "content_type": "text/plain; charset=utf-8",
                                "local_snapshot": str(local) if local else None,
                                "text": chunk,
                                "active_at_ingest": True,
                                **classified,
                            },
                            "official_docs_chunks",
                        )
                    chunk_counts[path] = emitted
                    stats["chunks_created"] += emitted

                materialized[path] = blob_sha
                content_hashes[path] = content_sha
                if previous_blob:
                    stats["modified_documents"] += 1
                else:
                    stats["new_documents"] += 1
                if replayed and upstream:
                    upstream.mark_unchanged(
                        source_id,
                        artifact,
                        signature=blob_sha,
                        url=page_url,
                        reason="LOCAL_CACHE_REPLAY",
                        extra={"git_commit": commit, "git_ref": ref, "git_path": path},
                    )
                if pause:
                    time.sleep(pause)
            except Exception as exc:
                stats["failed"] += 1
                stats["failures"].append({"path": path, "error": str(exc)[:1000]})
                if upstream:
                    upstream.mark_error(
                        source_id,
                        artifact,
                        url=f"https://github.com/{owner_repo}/blob/{commit}/{path}",
                        error=str(exc),
                        method="OFFICIAL_GIT_BLOB",
                    )

        state["git_known_paths_v1"] = sorted(current_paths if stats["discovery_complete"] else known_paths | current_paths)
        stats["backlog_count"] = stats["failed"] + stats["deferred_budget"] + stats["skipped_oversize"] + int(not stats["discovery_complete"])
        if stats["backlog_count"] == 0:
            state["git_last_complete_commit_v1"] = commit
        stats["business_chunks"] = sum(int(chunk_counts.get(path) or 0) for path in current_paths if path in materialized)
        selected = int(stats["selected_pages"] or 0)
        stats["incremental_efficiency"] = round((int(stats["body_requests_avoided"] or 0) / selected) * 100, 2) if selected else 0.0
        if snapshot_dir:
            atomic_write_json(snapshot_dir / "official_docs_sync_stats.json", stats)
        rid = hashlib.sha256(f"{source_id}|official-docs-stats".encode()).hexdigest()
        yield dlt.mark.with_table_name({"record_id": rid, "run_stats_json": json.dumps(stats, ensure_ascii=False), **stats}, "official_docs_sync_stats")

    return resource()
