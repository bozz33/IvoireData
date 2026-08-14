from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

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
    """Infer how a stable release maps to the documentation ref generically."""
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


def _ref_exists(repository: str, ref: str, user_agent: str) -> bool:
    import requests

    encoded = quote(ref, safe="")
    url = f"https://api.github.com/repos/{repository}/branches/{encoded}"
    response = requests.get(url, headers=_headers(user_agent), timeout=30)
    if response.status_code == 404:
        # Tags are valid canonical refs too.
        tag_url = f"https://api.github.com/repos/{repository}/git/ref/tags/{encoded}"
        response = requests.get(tag_url, headers=_headers(user_agent), timeout=30)
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True


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
                candidate = _ref_for_release(ref, str(release["tag"]), ref_strategy) or ref
                candidate_available = candidate == ref or _ref_exists(repository, candidate, user_agent)
                if candidate_available:
                    resolved_ref = candidate
                    status = "RESOLVED_CURRENT_STABLE"
                else:
                    resolved_ref = ref
                    status = "FALLBACK_CONFIGURED_REF"
                resolution.update({
                    "resolved_ref": resolved_ref,
                    "detected_version": str(release["tag"]).lstrip("vV"),
                    "detected_release": release.get("release_url"),
                    "release_published_at": release.get("published_at"),
                    "version_changed": resolved_ref != ref,
                    "status": status,
                })
                if not candidate_available:
                    resolution["fallback_reason"] = "RESOLVED_REF_UNAVAILABLE"
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

    return _base_git_resource(
        source_id=source_id,
        repository=repository,
        ref=resolved_ref,
        user_agent=user_agent,
        snapshot_dir=snapshot_dir,
        metadata_base=base,
        **kwargs,
    )


strategy.official_git_docs_resource = resolving_official_git_docs_resource
