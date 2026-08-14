from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..state_io import atomic_write_json
from . import official_docs_strategy as strategy
from .official_git_docs import official_git_docs_resource as _base_git_resource

_SEMVER = re.compile(r"^[^0-9]*(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_MAJOR_BRANCH = re.compile(r"^\d+\.x$")
_MINOR_BRANCH = re.compile(r"^\d+\.\d+$")


def _headers(user_agent: str) -> dict[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _semver_parts(tag: str) -> tuple[int, int | None, int | None] | None:
    match = _SEMVER.match((tag or "").strip())
    if not match:
        return None
    return (
        int(match.group(1)),
        int(match.group(2)) if match.group(2) is not None else None,
        int(match.group(3)) if match.group(3) is not None else None,
    )


def infer_ref_strategy(configured_ref: str) -> str:
    """Infer how an upstream release maps to the documentation ref.

    This is intentionally source-agnostic. Explicit metadata can override it for unusual
    repositories, but the common `13.x`, `5.x`, `13.3`, tag and main-branch layouts need
    no per-project code.
    """
    ref = str(configured_ref or "").strip()
    if _MAJOR_BRANCH.match(ref):
        return "major_branch"
    if _MINOR_BRANCH.match(ref):
        return "minor_branch"
    if ref.casefold() in {"main", "master", "stable", "current", "latest"}:
        return "fixed_ref"
    if _semver_parts(ref):
        return "release_tag"
    return "fixed_ref"


def _ref_for_release(configured_ref: str, tag: str, strategy_name: str) -> str:
    parts = _semver_parts(tag)
    if not parts:
        return configured_ref
    major, minor, _patch = parts
    if strategy_name == "major_branch":
        return f"{major}.x"
    if strategy_name == "minor_branch" and minor is not None:
        return f"{major}.{minor}"
    if strategy_name == "release_tag":
        return tag
    return configured_ref


def _latest_stable_release(repository: str, user_agent: str) -> dict[str, Any] | None:
    import requests

    url = f"https://api.github.com/repos/{repository}/releases/latest"
    response = requests.get(url, headers=_headers(user_agent), timeout=60)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        return None
    tag = str(payload.get("tag_name") or "").strip()
    if not tag or _semver_parts(tag) is None:
        return None
    return {
        "tag": tag,
        "published_at": payload.get("published_at"),
        "release_url": payload.get("html_url"),
    }


def _write_resolution(snapshot_dir: Path | None, payload: dict[str, Any]) -> None:
    if snapshot_dir is not None:
        atomic_write_json(snapshot_dir / "version_resolution.json", payload)


def resolving_official_git_docs_resource(
    *,
    source_id: str,
    repository: str,
    ref: str,
    user_agent: str = "IvoireData/0.8.3",
    snapshot_dir: Path | None = None,
    metadata_base: dict[str, Any] | None = None,
    **kwargs: Any,
):
    base = dict(metadata_base or {})
    policy = str(base.get("version_policy") or "CURRENT_STABLE").upper()

    # Generic defaults: the canonical docs repository also supplies releases. A source
    # may provide `version_repository` only when its docs live in a separate repository
    # (for example a dedicated docs repo). This is metadata, not source-specific code.
    version_repository = str(base.get("version_repository") or repository).strip()
    ref_strategy = str(base.get("version_ref_strategy") or infer_ref_strategy(ref)).strip()
    resolved_ref = ref
    resolution: dict[str, Any] = {
        "source_id": source_id,
        "version_policy": policy,
        "canonical_repository": repository,
        "configured_ref": ref,
        "resolved_ref": ref,
        "version_repository": version_repository,
        "ref_strategy": ref_strategy,
        "detected_version": None,
        "detected_release": None,
        "version_changed": False,
        "status": "CONFIGURED_REF",
        "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }

    if policy == "CURRENT_STABLE" and version_repository:
        try:
            release = _latest_stable_release(version_repository, user_agent)
            if release:
                candidate = _ref_for_release(ref, str(release["tag"]), ref_strategy)
                resolved_ref = candidate or ref
                resolution.update({
                    "resolved_ref": resolved_ref,
                    "detected_version": str(release["tag"]).lstrip("vV"),
                    "detected_release": release.get("release_url"),
                    "release_published_at": release.get("published_at"),
                    "version_changed": resolved_ref != ref,
                    "status": "RESOLVED_CURRENT_STABLE",
                })
            else:
                resolution["status"] = "NO_RELEASE_METADATA_USE_CONFIGURED_REF"
        except Exception as exc:
            resolution.update({
                "status": "FALLBACK_CONFIGURED_REF",
                "error": str(exc)[:1000],
            })

    base.update({
        "resolved_doc_ref": resolved_ref,
        "detected_doc_version": resolution.get("detected_version") or base.get("doc_version"),
        "version_resolution_status": resolution["status"],
    })
    _write_resolution(snapshot_dir, resolution)

    try:
        return _base_git_resource(
            source_id=source_id,
            repository=repository,
            ref=resolved_ref,
            user_agent=user_agent,
            snapshot_dir=snapshot_dir,
            metadata_base=base,
            **kwargs,
        )
    except Exception:
        # Automatic intelligence must degrade safely. If a detected stable ref does not
        # exist yet in the docs repository, retry the configured ref instead of breaking
        # ingestion. Overrides remain possible, but are never mandatory for normal repos.
        if resolved_ref != ref:
            resolution.update({
                "resolved_ref": ref,
                "version_changed": False,
                "status": "FALLBACK_CONFIGURED_REF",
                "fallback_reason": "RESOLVED_REF_UNAVAILABLE",
            })
            _write_resolution(snapshot_dir, resolution)
            base["resolved_doc_ref"] = ref
            base["version_resolution_status"] = resolution["status"]
            return _base_git_resource(
                source_id=source_id,
                repository=repository,
                ref=ref,
                user_agent=user_agent,
                snapshot_dir=snapshot_dir,
                metadata_base=base,
                **kwargs,
            )
        raise


# official_docs_strategy resolves this global at call time. Replacing it here keeps the
# historical `official_docs` connector and all existing runtime configuration compatible.
strategy.official_git_docs_resource = resolving_official_git_docs_resource
