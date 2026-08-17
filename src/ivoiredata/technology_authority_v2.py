from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import quote, urlparse

import requests

from .technology_authority import OfficialAuthorityResolver as _BaseAuthorityResolver
from .technology_discovery import (
    _deps_links,
    _extract_repository,
    normalize_registry,
    normalize_repository_url,
)
from .technology_qualification_v2 import (
    _is_registry_landing_url,
    _sanitize_native_for_policy,
)

RepositoryIdentityResolver = Callable[[str], dict[str, Any] | None]

_DOC_FIELDS = (
    "documentation_url",
    "documentation",
    "docs_url",
    "documentation_uri",
    "readme_url",
)
_HOMEPAGE_FIELDS = (
    "homepage",
    "homepage_url",
    "project_url",
)


def _github_slug(repository: str | None) -> tuple[str, str] | None:
    normalized = normalize_repository_url(repository)
    if not normalized:
        return None
    try:
        parsed = urlparse(normalized)
    except ValueError:
        return None
    if (parsed.hostname or "").casefold() != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def _contains_weak_landing(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_weak_landing(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_weak_landing(item) for item in value)
    return _is_registry_landing_url(value)


def _sanitize_ecosystems_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    for key in (*_DOC_FIELDS, *_HOMEPAGE_FIELDS):
        if key in cleaned and _contains_weak_landing(cleaned.get(key)):
            cleaned[key] = None
    metadata = cleaned.get("metadata")
    if isinstance(metadata, dict):
        cleaned["metadata"] = _sanitize_ecosystems_payload(metadata)
    return cleaned


def _sanitize_crosscheck_for_policy(cross: dict[str, Any]) -> dict[str, Any]:
    """Remove known package/aggregation landing pages from secondary URL evidence.

    Native Maven metadata is sanitized separately, but ecosyste.ms/deps.dev can expose
    generated package-documentation landing pages. Those are useful discovery hints,
    not evidence that the host is the project's official documentation authority.
    """
    cleaned = dict(cross)
    ecosystems = cross.get("ecosystems")
    if isinstance(ecosystems, dict):
        cleaned["ecosystems"] = _sanitize_ecosystems_payload(ecosystems)
    links = cross.get("deps_links")
    if isinstance(links, dict):
        links_copy = dict(links)
        for key in ("DOCUMENTATION", "HOMEPAGE"):
            if _contains_weak_landing(links_copy.get(key)):
                links_copy.pop(key, None)
        cleaned["deps_links"] = links_copy
    return cleaned


def _sanitize_authority_result_for_policy(result: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(result)
    evidence = list(cleaned.get("evidence") or [])
    if _is_registry_landing_url(cleaned.get("documentation_url")):
        cleaned["documentation_url"] = None
        if "REGISTRY_LANDING_REJECTED_AS_DOCUMENTATION" not in evidence:
            evidence.append("REGISTRY_LANDING_REJECTED_AS_DOCUMENTATION")
    if _is_registry_landing_url(cleaned.get("official_website")):
        cleaned["official_website"] = None
        if "REGISTRY_LANDING_REJECTED_AS_OFFICIAL_WEBSITE" not in evidence:
            evidence.append("REGISTRY_LANDING_REJECTED_AS_OFFICIAL_WEBSITE")
    cleaned["evidence"] = evidence
    return cleaned


class OfficialAuthorityResolver(_BaseAuthorityResolver):
    """Authority resolver v2 with repository-transfer reconciliation.

    Independent sources can lag behind a legitimate GitHub repository transfer or
    rename. Textual URL inequality is therefore not sufficient to call a supply-chain
    conflict. When (and only when) GitHub repository URLs disagree, this resolver asks
    GitHub for their immutable repository identity. Equal repository IDs convert the
    apparent conflict into corroborating evidence; different IDs remain blocked.

    Legacy Maven metadata is also policy-sanitized before every authority decision so
    an old persisted Central artifact landing page can never be reintroduced as a
    project website or documentation URL after qualification recalibration removed it.
    """

    def __init__(
        self,
        *,
        repository_identity_resolver: RepositoryIdentityResolver | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.repository_identity_resolver = repository_identity_resolver
        self._repository_identity_cache: dict[str, dict[str, Any] | None] = {}

    def _github_identity(self, repository: str) -> dict[str, Any] | None:
        normalized = normalize_repository_url(repository)
        if not normalized:
            return None
        if normalized in self._repository_identity_cache:
            return self._repository_identity_cache[normalized]
        if self.repository_identity_resolver is not None:
            identity = self.repository_identity_resolver(normalized)
            self._repository_identity_cache[normalized] = identity
            return identity

        slug = _github_slug(normalized)
        if slug is None:
            self._repository_identity_cache[normalized] = None
            return None
        owner, repo = slug
        url = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        try:
            response = self.session.get(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/vnd.github+json",
                },
                timeout=30,
                allow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("id") in (None, ""):
                identity = None
            else:
                html_url = normalize_repository_url(payload.get("html_url"))
                identity = {
                    "id": str(payload["id"]),
                    "full_name": str(payload.get("full_name") or ""),
                    "html_url": html_url or normalized,
                }
        except (requests.RequestException, ValueError):
            identity = None
        self._repository_identity_cache[normalized] = identity
        return identity

    def _reconcile_repository_transfer(
        self,
        native: dict[str, Any],
        cross: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        eco = cross.get("ecosystems") if isinstance(cross.get("ecosystems"), dict) else {}
        links = cross.get("deps_links") if isinstance(cross.get("deps_links"), dict) else {}
        native_repo = normalize_repository_url(native.get("canonical_repository"))
        eco_repo = normalize_repository_url(_extract_repository(eco))
        deps_repo = normalize_repository_url(
            links.get("SOURCE_REPO") or links.get("SOURCE") or links.get("REPOSITORY")
        )
        repositories = [repo for repo in (native_repo, eco_repo, deps_repo) if repo]
        if len(set(repositories)) <= 1 or not repositories:
            return native, cross, None
        if any(_github_slug(repo) is None for repo in repositories):
            return native, cross, None

        identities = [(repo, self._github_identity(repo)) for repo in repositories]
        resolved = [(repo, identity) for repo, identity in identities if identity is not None]
        if len(resolved) != len(repositories):
            return native, cross, None
        ids = {str(identity["id"]) for _, identity in resolved}
        if len(ids) != 1:
            return native, cross, None

        canonical_identity = next(
            (
                identity
                for repo, identity in resolved
                if repo == native_repo and identity.get("html_url")
            ),
            resolved[0][1],
        )
        canonical_repo = normalize_repository_url(canonical_identity.get("html_url")) or native_repo
        if not canonical_repo:
            return native, cross, None

        reconciled_native = dict(native)
        reconciled_native["canonical_repository"] = canonical_repo
        reconciled_cross = dict(cross)
        if eco:
            eco_copy = dict(eco)
            eco_copy["repository_url"] = canonical_repo
            reconciled_cross["ecosystems"] = eco_copy
        if links:
            links_copy = dict(links)
            for key in ("SOURCE_REPO", "SOURCE", "REPOSITORY"):
                if links_copy.get(key):
                    links_copy[key] = canonical_repo
            reconciled_cross["deps_links"] = links_copy

        evidence = {
            "id": str(canonical_identity["id"]),
            "canonical_repository": canonical_repo,
            "aliases": sorted(set(repositories)),
        }
        return reconciled_native, reconciled_cross, evidence

    def _decision(
        self,
        row: dict[str, Any],
        native: dict[str, Any],
        cross: dict[str, Any],
    ) -> dict[str, Any]:
        policy_native = _sanitize_native_for_policy(str(row.get("registry") or ""), native)
        policy_cross = _sanitize_crosscheck_for_policy(cross)
        first = _sanitize_authority_result_for_policy(
            super()._decision(row, policy_native, policy_cross)
        )
        if first.get("authority_status") != "AUTHORITY_CONFLICT":
            return first

        reconciled_native, reconciled_cross, transfer = self._reconcile_repository_transfer(
            policy_native,
            policy_cross,
        )
        if transfer is None:
            return first
        result = _sanitize_authority_result_for_policy(
            super()._decision(row, reconciled_native, reconciled_cross)
        )
        evidence = list(result.get("evidence") or [])
        marker = "GITHUB_REPOSITORY_TRANSFER_MATCH"
        if marker not in evidence:
            evidence.append(marker)
        result["evidence"] = evidence
        result["canonical_repository"] = transfer["canonical_repository"]
        result["repository_conflict"] = False
        result["repository_match"] = True
        result["repository_transfer"] = transfer
        return result

    def recheck_conflicts(
        self,
        *,
        limit: int = 25,
        registry: str | None = None,
    ) -> dict[str, Any]:
        """Re-evaluate persisted conflicts without touching native registries.

        Native package metadata is reused from qualification_results.metadata_json.
        Secondary cross-checks and GitHub identity resolution are refreshed because
        those external authorities may have caught up with a repository transfer.
        """
        if int(limit) <= 0:
            raise ValueError("authority conflict recheck is intentionally bounded; --limit must be > 0")
        normalized = normalize_registry(registry) if registry else None
        where = ["a.authority_status='AUTHORITY_CONFLICT'"]
        params: list[Any] = []
        if normalized:
            where.append("a.registry=?")
            params.append(normalized)
        params.append(int(limit))
        rows = self.db.execute(
            """
            SELECT q.*
            FROM authority_results AS a
            JOIN qualification_results AS q
              ON q.registry=a.registry AND q.name=a.name
            WHERE """
            + " AND ".join(where)
            + " ORDER BY a.last_checked_at ASC,a.registry ASC,a.name ASC LIMIT ?",
            params,
        ).fetchall()

        outcomes: list[dict[str, Any]] = []
        by_status: dict[str, int] = {}
        for raw in rows:
            row = dict(raw)
            try:
                native = json.loads(str(row.get("metadata_json") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                native = {}
            if not isinstance(native, dict):
                native = {}
            cross = self._crosscheck(row, native)
            result = self._save(self._decision(row, native, cross))
            status = str(result.get("authority_status") or "UNKNOWN")
            by_status[status] = by_status.get(status, 0) + 1
            outcomes.append(
                {
                    "registry": result.get("registry"),
                    "name": result.get("canonical_name") or result.get("name"),
                    "status": status,
                    "repository": result.get("canonical_repository"),
                    "repository_match": result.get("repository_match"),
                    "repository_conflict": result.get("repository_conflict"),
                    "repository_transfer": result.get("repository_transfer"),
                }
            )
        return {
            "engine": "official-authority-v2",
            "rechecked": len(outcomes),
            "by_status": dict(sorted(by_status.items())),
            "resolved_conflicts": sum(
                1 for item in outcomes if item["status"] != "AUTHORITY_CONFLICT"
            ),
            "remaining_conflicts": by_status.get("AUTHORITY_CONFLICT", 0),
            "outcomes": outcomes[:100],
        }
