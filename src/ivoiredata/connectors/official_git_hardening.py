from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlparse

from ..metadata import classify_from_base, title_from_text
from ..snapshots import save_snapshot
from ..state_io import atomic_write_json
from ..upstream_state import UpstreamState
from . import official_docs as docs_base
from . import official_docs_strategy as strategy
from . import official_git_docs as git_base
from . import official_git_versions as versions
from .public_web import chunk_text


class GitHubRateLimitError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        retry_after_seconds: int | None = None,
        rate_limit_reset: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.retry_after_seconds = retry_after_seconds
        self.rate_limit_reset = rate_limit_reset


def parse_github_tree_target(url: str) -> tuple[str, str, str | None] | None:
    """Return ``(owner/repo, ref, path_prefix)`` for a GitHub tree URL.

    ``parse_github_tree_url`` historically kept only repository and ref.  That loses
    the most important part of an Active Docs target such as ``/tree/3.x/docs`` and
    causes the Git connector to crawl every Markdown file in the repository.  Keep the
    subdirectory as a normalized filesystem prefix instead.
    """

    parsed = urlparse(str(url or ""))
    if (parsed.hostname or "").casefold() not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[2] != "tree":
        return None
    repository = f"{parts[0]}/{parts[1]}"
    ref = parts[3]
    path = "/".join(parts[4:]).strip("/")
    prefix = path.rstrip("/") + "/" if path else None
    return repository, ref, prefix


def _retry_after_seconds(response: Any) -> int | None:
    headers = getattr(response, "headers", {}) or {}
    value = str(headers.get("Retry-After") or headers.get("retry-after") or "").strip()
    if value:
        try:
            return max(1, int(float(value)))
        except (TypeError, ValueError):
            pass
    reset = str(headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset") or "").strip()
    if reset:
        try:
            delay = int(float(reset)) - int(time.time()) + 1
            return max(1, delay)
        except (TypeError, ValueError):
            pass
    return None


def _rate_limit_reset(response: Any) -> str | None:
    headers = getattr(response, "headers", {}) or {}
    reset = str(headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset") or "").strip()
    if not reset:
        return None
    try:
        return datetime.fromtimestamp(int(float(reset)), tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return reset[:100]


def _is_rate_limited(response: Any) -> bool:
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 429:
        return True
    if status != 403:
        return False
    headers = getattr(response, "headers", {}) or {}
    remaining = str(headers.get("X-RateLimit-Remaining") or headers.get("x-ratelimit-remaining") or "").strip()
    if remaining == "0":
        return True
    try:
        text = str(getattr(response, "text", "") or "").casefold()
    except Exception:
        text = ""
    return "rate limit" in text or "secondary rate" in text


def _raise_if_rate_limited(response: Any, url: str) -> None:
    if not _is_rate_limited(response):
        return
    status = int(getattr(response, "status_code", 0) or 0)
    retry_after = _retry_after_seconds(response)
    reset = _rate_limit_reset(response)
    raise GitHubRateLimitError(
        f"GitHub rate limit while fetching {url} (HTTP {status})",
        status_code=status,
        retry_after_seconds=retry_after,
        rate_limit_reset=reset,
    )


def _fetch_blob_body(
    session: Any,
    *,
    owner_repo: str,
    commit: str,
    path: str,
    blob_sha: str,
    api_headers: dict[str, str],
    user_agent: str,
) -> tuple[bytes, str, str]:
    """Fetch one Git blob using the official REST transport when authenticated.

    The recursive Git tree already gives us the immutable blob SHA.  With a GitHub
    token, use ``GET /git/blobs/{sha}`` with GitHub's raw media type so the body stays
    on the authenticated REST quota.  Anonymous operation keeps the public raw host,
    but rate-limit responses are surfaced to the caller so it can open a run-level
    circuit breaker instead of hammering the host for every remaining file.
    """

    if api_headers.get("Authorization"):
        url = f"https://api.github.com/repos/{owner_repo}/git/blobs/{quote(blob_sha, safe='')}"
        headers = dict(api_headers)
        headers["Accept"] = "application/vnd.github.raw+json"
        response = session.get(url, headers=headers, timeout=120)
        _raise_if_rate_limited(response, url)
        response.raise_for_status()
        return bytes(response.content or b""), url, "GITHUB_GIT_BLOB_API_AUTHENTICATED"

    url = f"https://raw.githubusercontent.com/{owner_repo}/{commit}/{quote(path, safe='/')}"
    response = session.get(url, headers={"User-Agent": user_agent}, timeout=120)
    _raise_if_rate_limited(response, url)
    response.raise_for_status()
    return bytes(response.content or b""), url, "RAW_GITHUB_ANONYMOUS"


def hardened_official_git_docs_resource(
    *,
    source_id: str,
    repository: str,
    ref: str,
    user_agent: str = "IvoireData/0.8.4",
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
    """Incrementally materialize official Git documentation with scoped, rate-safe IO."""

    import dlt
    import requests

    max_pages = max(1, int(max_pages))
    max_bytes_per_page = max(100_000, int(max_bytes_per_page))
    max_new_bytes_per_run = max(0, int(max_new_bytes_per_run))
    pause = max(0.0, min(float(request_pause_seconds), 2.0))
    prefixes = [str(value).strip("/") + "/" for value in include_prefixes if str(value).strip("/")]
    prefixes = list(dict.fromkeys(prefixes))
    excludes = [git_base.re.compile(str(value), git_base.re.I) for value in exclude_patterns if str(value)]
    exclude_text = [pattern.pattern for pattern in excludes]
    base = dict(metadata_base or {})

    @dlt.resource(name="official_docs", write_disposition="merge", primary_key="record_id")
    def resource():
        session = requests.Session()
        headers = git_base._api_headers(user_agent)
        authenticated = bool(headers.get("Authorization"))
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

        scope_signature = json.dumps(
            {
                "repository": owner_repo.casefold(),
                "ref": str(ref),
                "include_prefixes": prefixes,
                "exclude_patterns": exclude_text,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if str(state.get("git_scope_signature_v2") or "") != scope_signature:
            # Scope changes must never take the unchanged-commit fast path.  A target
            # narrowed from repository root to docs/ has to re-evaluate the tree even
            # if the commit itself did not change.
            last_complete_commit = ""

        encoded_ref = quote(ref, safe="")
        branch_url = f"https://api.github.com/repos/{owner_repo}/branches/{encoded_ref}"
        branch = git_base._get_json(session, branch_url, headers)
        commit = str(((branch.get("commit") or {}).get("sha")) or "")
        tree_sha = str((((branch.get("commit") or {}).get("commit") or {}).get("tree") or {}).get("sha") or "")
        if not commit or not tree_sha:
            raise RuntimeError(f"cannot resolve GitHub commit/tree for {owner_repo}@{ref}")

        repo_root = f"https://github.com/{owner_repo}/tree/{ref}"
        scoped_root = repo_root
        if len(prefixes) == 1:
            scoped_root = repo_root + "/" + prefixes[0].rstrip("/")
        transport = "GITHUB_GIT_BLOB_API_AUTHENTICATED" if authenticated else "RAW_GITHUB_ANONYMOUS"
        stats: dict[str, Any] = {
            "source_id": source_id,
            "root_url": scoped_root,
            "final_root_url": scoped_root,
            "source_strategy": "OFFICIAL_GIT",
            "git_repository": owner_repo,
            "git_ref": ref,
            "git_commit": commit,
            "git_tree": tree_sha,
            "git_include_prefixes": prefixes,
            "git_exclude_patterns": exclude_text,
            "git_blob_transport": transport,
            "github_authenticated": authenticated,
            "github_rate_limited": False,
            "github_retry_after_seconds": None,
            "github_rate_limit_reset": None,
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
            "deferred_rate_limit": 0,
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

        if last_complete_commit == commit and materialized:
            scoped_materialized = {
                path: blob_sha
                for path, blob_sha in materialized.items()
                if git_base._path_allowed(path, prefixes, excludes)
            }
            stats["discovered_pages"] = len(scoped_materialized)
            stats["selected_pages"] = len(scoped_materialized)
            stats["unchanged_git"] = len(scoped_materialized)
            stats["body_requests_avoided"] = len(scoped_materialized)
            stats["chunks_reused"] = sum(int(chunk_counts.get(path) or 0) for path in scoped_materialized)
            stats["business_chunks"] = stats["chunks_reused"]
            stats["incremental_efficiency"] = 100.0
            for path, blob_sha in scoped_materialized.items():
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
            yield dlt.mark.with_table_name(
                {"record_id": rid, "run_stats_json": json.dumps(stats, ensure_ascii=False), **stats},
                "official_docs_sync_stats",
            )
            return

        tree_url = f"https://api.github.com/repos/{owner_repo}/git/trees/{tree_sha}?recursive=1"
        tree = git_base._get_json(session, tree_url, headers)
        if tree.get("truncated"):
            stats["discovery_complete"] = False
            stats["discovery_truncated"] = True

        entries: list[dict[str, Any]] = []
        for item in tree.get("tree", []):
            if not isinstance(item, dict) or item.get("type") != "blob":
                continue
            path = str(item.get("path") or "")
            if not path or not git_base._path_allowed(path, prefixes, excludes):
                continue
            entries.append(
                {
                    "path": path,
                    "sha": str(item.get("sha") or ""),
                    "size": int(item.get("size") or 0),
                }
            )
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
        for index, item in enumerate(entries):
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
                stats["failures"].append(
                    {"path": path, "error": "PAGE_TOO_LARGE", "size": size, "limit": max_bytes_per_page}
                )
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
                    fetch_url = f"https://github.com/{owner_repo}/blob/{commit}/{path}"
                else:
                    raw, fetch_url, used_transport = _fetch_blob_body(
                        session,
                        owner_repo=owner_repo,
                        commit=commit,
                        path=path,
                        blob_sha=blob_sha,
                        api_headers=headers,
                        user_agent=user_agent,
                    )
                    stats["git_blob_transport"] = used_transport
                    if len(raw) > max_bytes_per_page:
                        stats["skipped_oversize"] += 1
                        stats["failures"].append(
                            {"path": path, "error": "PAGE_TOO_LARGE", "size": len(raw), "limit": max_bytes_per_page}
                        )
                        continue
                    budget = max(0, budget - len(raw))
                    stats["downloaded"] += 1
                    stats["downloaded_bytes"] += len(raw)
                    snap = save_snapshot(
                        snapshot_dir,
                        source_id=source_id,
                        url=fetch_url,
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
                            url=fetch_url,
                            signature=blob_sha,
                            sha256=raw_sha,
                            size_bytes=len(raw),
                            method="OFFICIAL_GIT_BLOB_API" if authenticated else "OFFICIAL_GIT_BLOB_RAW",
                            local_path=str(local) if local else None,
                            extra={
                                "git_commit": commit,
                                "git_ref": ref,
                                "git_path": path,
                                "git_blob_sha": blob_sha,
                                "transport": used_transport,
                            },
                        )

                text = git_base._extract_text(raw)
                if not text:
                    stats["failed"] += 1
                    stats["failures"].append({"path": path, "error": "NO_EXTRACTABLE_TEXT"})
                    continue
                raw_sha = hashlib.sha256(raw).hexdigest()
                content_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                page_url = f"https://github.com/{owner_repo}/blob/{commit}/{path}"
                classified = classify_from_base(
                    base,
                    page_url,
                    text,
                    document_type="DEVELOPER_DOCUMENTATION",
                )
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
                    for chunk_index, chunk in enumerate(chunk_text(text, size=5000, overlap=300)):
                        chunk_id = hashlib.sha256(
                            f"{source_id}|{path}|{content_sha}|{chunk_index}".encode()
                        ).hexdigest()
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
                                "chunk_index": chunk_index,
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
            except GitHubRateLimitError as exc:
                stats["failed"] += 1
                stats["github_rate_limited"] = True
                stats["github_retry_after_seconds"] = exc.retry_after_seconds
                stats["github_rate_limit_reset"] = exc.rate_limit_reset
                stats["deferred_rate_limit"] = max(0, len(entries) - index - 1)
                stats["failures"].append(
                    {
                        "path": path,
                        "error": str(exc)[:1000],
                        "status_code": exc.status_code,
                        "retry_after_seconds": exc.retry_after_seconds,
                        "rate_limit_reset": exc.rate_limit_reset,
                    }
                )
                if upstream:
                    upstream.mark_error(
                        source_id,
                        artifact,
                        url=f"https://github.com/{owner_repo}/blob/{commit}/{path}",
                        error=str(exc),
                        method="OFFICIAL_GIT_RATE_LIMIT",
                    )
                # Shared run-level circuit breaker: one rate-limit response is enough.
                # Do not turn the rest of the repository into a storm of identical 429s.
                break
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

        state["git_known_paths_v1"] = sorted(
            current_paths if stats["discovery_complete"] else known_paths | current_paths
        )
        state["git_scope_signature_v2"] = scope_signature
        stats["backlog_count"] = (
            stats["failed"]
            + stats["deferred_budget"]
            + stats["deferred_rate_limit"]
            + stats["skipped_oversize"]
            + int(not stats["discovery_complete"])
        )
        if stats["backlog_count"] == 0:
            state["git_last_complete_commit_v1"] = commit
        stats["business_chunks"] = sum(
            int(chunk_counts.get(path) or 0)
            for path in current_paths
            if path in materialized
        )
        selected = int(stats["selected_pages"] or 0)
        stats["incremental_efficiency"] = (
            round((int(stats["body_requests_avoided"] or 0) / selected) * 100, 2)
            if selected
            else 0.0
        )
        if snapshot_dir:
            atomic_write_json(snapshot_dir / "official_docs_sync_stats.json", stats)
        rid = hashlib.sha256(f"{source_id}|official-docs-stats".encode()).hexdigest()
        yield dlt.mark.with_table_name(
            {"record_id": rid, "run_stats_json": json.dumps(stats, ensure_ascii=False), **stats},
            "official_docs_sync_stats",
        )

    return resource()


_original_strategy_resource = strategy.official_docs_resource


def hardened_official_docs_resource(*, source_id: str, url: str, **kwargs: Any):
    """Preserve a canonical GitHub tree subdirectory as an effective Git prefix."""

    parsed = parse_github_tree_target(url)
    if parsed is None:
        return _original_strategy_resource(source_id=source_id, url=url, **kwargs)

    repository, ref, discovered_prefix = parsed
    snapshot_dir = kwargs.get("snapshot_dir")
    metadata = dict(kwargs.get("metadata_base") or {})
    configured = [str(value) for value in kwargs.get("include_prefixes", ()) if str(value)]
    filesystem_prefixes = [value for value in configured if not value.startswith("/")]
    prefixes = [discovered_prefix] if discovered_prefix else filesystem_prefixes
    strategy._write_strategy(
        snapshot_dir,
        {
            "source_id": source_id,
            "strategy": "OFFICIAL_GIT",
            "reason": "CANONICAL_GIT_URL",
            "repository": repository,
            "ref": ref,
            "canonical_tree_prefix": discovered_prefix,
            "effective_include_prefixes": prefixes,
            "official_url": metadata.get("public_docs_url") or url,
        },
    )
    return strategy._git_resource(
        source_id=source_id,
        repository=repository,
        ref=ref,
        kwargs=kwargs,
        include_prefixes=prefixes,
    )


# Apply after official_docs_strategy and official_git_versions have loaded.  The strategy
# still owns web-vs-Git routing and version resolution; this module only hardens the two
# production defects observed by the PR #38 canary.
versions._base_git_resource = hardened_official_git_docs_resource
strategy.official_docs_resource = hardened_official_docs_resource
docs_base.official_docs_resource = hardened_official_docs_resource
