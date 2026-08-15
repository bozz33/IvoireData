from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests

from .technology_discovery import (
    DEPS_DEV_API,
    ECOSYSTEMS_API,
    GlobalTechnologyDiscoveryEngine,
    _DEPS_SYSTEMS,
    _deps_links,
    _extract_docs,
    _extract_repository,
    _first,
    normalize_registry,
    normalize_repository_url,
    officiality_score,
    officiality_status,
)
from .technology_registries import build_purl, importance_score, native_package_metadata


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _merge_unique(*values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        items = value if isinstance(value, list) else [value]
        for item in items:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
    return out


class GlobalTechnologyCatalogEngine(GlobalTechnologyDiscoveryEngine):
    """Phase-2 global technology catalog with native authority and identity reconciliation."""

    def _safe_json(self, url: str) -> dict[str, Any]:
        try:
            payload = self._json(url)
        except (requests.RequestException, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def discover_package(self, registry: str, name: str) -> dict[str, Any]:
        registry = normalize_registry(registry)

        native: dict[str, Any] = {}
        try:
            native = native_package_metadata(registry, name, session=self.session, user_agent=self.user_agent) or {}
        except (requests.RequestException, ValueError, KeyError):
            native = {}

        eco_url = f"{ECOSYSTEMS_API}/registries/{quote(registry, safe='')}/packages/{quote(str(name).strip(), safe='')}"
        eco = self._safe_json(eco_url)

        canonical_name = str(native.get("name") or _first(eco, "name", "package_name") or name)
        latest = native.get("latest_stable_version") or _first(eco, "latest_release_number", "latest_version", "version")
        latest = str(latest).strip() if latest else None

        registry_repo = normalize_repository_url(native.get("canonical_repository") or _extract_repository(eco))
        homepage = native.get("official_website") or _first(eco, "homepage", "homepage_url", "project_url")
        homepage = str(homepage) if homepage else None
        docs = native.get("documentation_url") or _extract_docs(eco)

        deps_repo = None
        deps_system = _DEPS_SYSTEMS.get(registry)
        if deps_system:
            deps_url = f"{DEPS_DEV_API}/systems/{deps_system}/packages/{quote(canonical_name, safe='')}"
            deps_package = self._safe_json(deps_url)
            versions = deps_package.get("versions") or []
            default = next((item for item in versions if isinstance(item, dict) and item.get("isDefault")), None)
            if default:
                version_key = default.get("versionKey") or {}
                default_version = str(version_key.get("version") or "").strip()
                if default_version and not latest:
                    latest = default_version
                if default_version:
                    version_url = f"{DEPS_DEV_API}/systems/{deps_system}/packages/{quote(canonical_name, safe='')}/versions/{quote(default_version, safe='')}"
                    deps_payload = self._safe_json(version_url)
                    links = _deps_links(deps_payload)
                    deps_repo = normalize_repository_url(links.get("SOURCE_REPO") or links.get("SOURCE") or links.get("REPOSITORY"))
                    docs = docs or links.get("DOCUMENTATION")
                    homepage = homepage or links.get("HOMEPAGE")

        score, evidence = officiality_score(
            registry_repo=registry_repo,
            deps_repo=deps_repo,
            homepage=homepage,
            docs=docs,
            version=latest,
        )
        if native:
            score = min(100, score + 10)
            evidence = _merge_unique("NATIVE_REGISTRY_METADATA", evidence)
        if registry_repo and deps_repo and normalize_repository_url(registry_repo) == normalize_repository_url(deps_repo):
            evidence = _merge_unique(evidence, "CROSS_SOURCE_REPOSITORY_MATCH")

        metrics = {
            "downloads_total": _int(native.get("downloads_total") or eco.get("downloads")),
            "downloads_recent": _int(native.get("downloads_recent") or eco.get("recent_downloads")),
            "dependents_count": _int(native.get("dependents_count") or eco.get("dependents_count")),
            "dependent_repos_count": _int(eco.get("dependent_repos_count")),
            "repository_stars": _int(eco.get("repository_stars") or eco.get("stars")),
            "favorites_count": _int(native.get("favorites_count") or eco.get("favers")),
        }

        purl = build_purl(registry, canonical_name)
        key = purl or f"package:{registry}:{canonical_name.casefold()}"
        record: dict[str, Any] = {
            "name": canonical_name,
            "category": "PACKAGE",
            "registry": registry,
            "purl": purl,
            "latest_stable_version": latest,
            "latest_purl": build_purl(registry, canonical_name, latest) if latest else None,
            "canonical_repository": registry_repo or deps_repo,
            "documentation_url": docs,
            "official_website": homepage,
            "officiality_score": score,
            "officiality_status": officiality_status(score),
            "officiality_evidence": evidence,
            "authority_source": native.get("authority_source") or "ecosyste.ms",
            "native_registry_url": native.get("native_registry_url"),
            "ecosystems_url": eco_url if eco else None,
            "discovery_sources": _merge_unique(native.get("authority_source"), "ecosyste.ms" if eco else None, "deps.dev" if deps_system else None),
            **metrics,
        }
        importance, tier = importance_score(record)
        record["importance_score"] = importance
        record["importance_tier"] = tier
        saved = self._upsert(key, record)
        self.data.setdefault("runs", []).append({
            "kind": "package-v2",
            "registry": registry,
            "name": canonical_name,
            "authority": record["authority_source"],
            "at": saved.get("last_seen_at"),
        })
        self.reconcile_identities(save=False)
        self._save()
        return saved

    def reconcile_identities(self, *, save: bool = True) -> dict[str, Any]:
        technologies = self.data.get("technologies", {})
        by_repo: dict[str, list[str]] = {}
        for tech_id, item in technologies.items():
            if not isinstance(item, dict):
                continue
            repo = normalize_repository_url(item.get("canonical_repository"))
            if repo:
                by_repo.setdefault(repo, []).append(tech_id)

        groups: dict[str, Any] = {}
        merged_aliases = 0
        ambiguous = 0
        for repo, members in sorted(by_repo.items()):
            if len(members) < 2:
                continue
            purls = [member for member in members if member.startswith("pkg:")]
            non_packages = [member for member in members if not member.startswith("pkg:")]
            group_id = "repo:" + repo.removeprefix("https://").removeprefix("http://")
            status = "LINKED"
            canonical = None
            if len(purls) == 1:
                canonical = purls[0]
                status = "MERGED_ALIAS"
                base = technologies[canonical]
                aliases = _merge_unique(base.get("identity_aliases"), *non_packages)
                qids = _merge_unique(base.get("wikidata_qids"), *[technologies[m].get("qid") for m in non_packages if isinstance(technologies.get(m), dict)])
                for member in non_packages:
                    other = technologies[member]
                    base["documentation_url"] = base.get("documentation_url") or other.get("documentation_url")
                    base["official_website"] = base.get("official_website") or other.get("official_website")
                    base["latest_stable_version"] = base.get("latest_stable_version") or other.get("latest_stable_version")
                    base["discovery_sources"] = _merge_unique(base.get("discovery_sources"), other.get("discovery_sources"))
                    other["canonical_technology_id"] = canonical
                    other["identity_status"] = "ALIAS_OF_PACKAGE"
                    merged_aliases += 1
                if aliases:
                    base["identity_aliases"] = aliases
                if qids:
                    base["wikidata_qids"] = qids
                base["identity_status"] = "CANONICAL"
            elif len(purls) > 1:
                status = "MONOREPO_AMBIGUOUS"
                ambiguous += 1
                for member in members:
                    technologies[member]["identity_status"] = status
                    technologies[member]["identity_group"] = group_id
            else:
                canonical = max(members, key=lambda member: int(technologies[member].get("officiality_score") or 0))
                technologies[canonical]["identity_status"] = "CANONICAL_NON_PACKAGE"
                for member in members:
                    if member != canonical:
                        technologies[member]["canonical_technology_id"] = canonical
                        technologies[member]["identity_status"] = "ALIAS_OF_ENTITY"
                        merged_aliases += 1
            groups[group_id] = {
                "repository": repo,
                "members": members,
                "package_members": purls,
                "canonical_technology_id": canonical,
                "status": status,
            }

        self.data["identity_groups"] = groups
        payload = {
            "groups": len(groups),
            "merged_aliases": merged_aliases,
            "monorepo_ambiguous_groups": ambiguous,
        }
        if save:
            self.data.setdefault("runs", []).append({"kind": "reconcile", **payload})
            self._save()
        return payload

    def refresh_packages(self, *, limit: int = 0) -> dict[str, Any]:
        items = [
            item for item in self.data.get("technologies", {}).values()
            if isinstance(item, dict) and item.get("registry") and item.get("name")
        ]
        items.sort(key=lambda item: (-int(item.get("importance_score") or 0), str(item.get("name") or "").casefold()))
        if limit > 0:
            items = items[:limit]
        success = 0
        failures: list[dict[str, str]] = []
        for item in items:
            try:
                self.discover_package(str(item["registry"]), str(item["name"]))
                success += 1
            except Exception as exc:  # one registry must not stop the global refresh
                failures.append({"registry": str(item.get("registry")), "name": str(item.get("name")), "error": str(exc)[:500]})
        reconcile = self.reconcile_identities(save=False)
        self.data.setdefault("runs", []).append({"kind": "refresh", "selected": len(items), "success": success, "failed": len(failures)})
        self._save()
        return {"selected": len(items), "success": success, "failed": len(failures), "failures": failures[:50], "reconcile": reconcile}

    def audit(self) -> dict[str, Any]:
        payload = super().audit()
        items = [value for value in self.data.get("technologies", {}).values() if isinstance(value, dict)]
        by_importance: dict[str, int] = {}
        by_authority: dict[str, int] = {}
        canonical = 0
        aliases = 0
        ambiguous = 0
        for item in items:
            tier = str(item.get("importance_tier") or "UNSCORED")
            by_importance[tier] = by_importance.get(tier, 0) + 1
            authority = str(item.get("authority_source") or "UNKNOWN")
            by_authority[authority] = by_authority.get(authority, 0) + 1
            status = str(item.get("identity_status") or "")
            canonical += status.startswith("CANONICAL")
            aliases += status.startswith("ALIAS")
            ambiguous += status == "MONOREPO_AMBIGUOUS"
        payload.update({
            "identity_groups": len(self.data.get("identity_groups", {})),
            "canonical_entities": canonical,
            "identity_aliases": aliases,
            "ambiguous_members": ambiguous,
            "by_importance_tier": dict(sorted(by_importance.items())),
            "by_authority_source": dict(sorted(by_authority.items())),
        })
        return payload

    def catalog(self, *, limit: int = 100, verified_only: bool = False, min_importance: int = 0) -> list[dict[str, Any]]:
        items = super().catalog(limit=0, verified_only=verified_only)
        if min_importance > 0:
            items = [item for item in items if int(item.get("importance_score") or 0) >= min_importance]
        items.sort(key=lambda item: (-int(item.get("importance_score") or 0), -int(item.get("officiality_score") or 0), str(item.get("name") or "").casefold()))
        return items[: max(0, int(limit))] if limit else items
