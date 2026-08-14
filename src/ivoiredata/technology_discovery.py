from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlparse

import requests

from .state_io import atomic_write_json, load_json

ECOSYSTEMS_API = "https://packages.ecosyste.ms/api/v1"
DEPS_DEV_API = "https://api.deps.dev/v3"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
LINGUIST_LANGUAGES = "https://raw.githubusercontent.com/github-linguist/linguist/main/lib/linguist/languages.yml"

_REGISTRY_ALIASES = {
    "npm": "npmjs.org",
    "npmjs": "npmjs.org",
    "npmjs.org": "npmjs.org",
    "pypi": "pypi.org",
    "pypi.org": "pypi.org",
    "packagist": "packagist.org",
    "packagist.org": "packagist.org",
    "cargo": "crates.io",
    "crates": "crates.io",
    "crates.io": "crates.io",
    "rubygems": "rubygems.org",
    "rubygems.org": "rubygems.org",
    "nuget": "nuget.org",
    "nuget.org": "nuget.org",
    "maven": "repo1.maven.org",
    "repo1.maven.org": "repo1.maven.org",
    "go": "proxy.golang.org",
    "golang": "proxy.golang.org",
    "proxy.golang.org": "proxy.golang.org",
    "pub": "pub.dev",
    "pub.dev": "pub.dev",
    "hex": "hex.pm",
    "hex.pm": "hex.pm",
    "hackage": "hackage.haskell.org",
    "hackage.haskell.org": "hackage.haskell.org",
    "cran": "cran.r-project.org",
    "cran.r-project.org": "cran.r-project.org",
}

_PURL_TYPES = {
    "npmjs.org": "npm",
    "pypi.org": "pypi",
    "packagist.org": "composer",
    "crates.io": "cargo",
    "rubygems.org": "gem",
    "nuget.org": "nuget",
    "repo1.maven.org": "maven",
    "proxy.golang.org": "golang",
    "pub.dev": "pub",
    "hex.pm": "hex",
    "hackage.haskell.org": "hackage",
    "cran.r-project.org": "cran",
}

_DEPS_SYSTEMS = {
    "npmjs.org": "NPM",
    "pypi.org": "PYPI",
    "crates.io": "CARGO",
    "rubygems.org": "RUBYGEMS",
    "nuget.org": "NUGET",
    "repo1.maven.org": "MAVEN",
    "proxy.golang.org": "GO",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_registry(value: str) -> str:
    key = str(value or "").strip().casefold()
    return _REGISTRY_ALIASES.get(key, key)


def normalize_repository_url(value: str | None) -> str | None:
    if not value:
        return None
    url = str(value).strip()
    if url.startswith("git+"):
        url = url[4:]
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.split(":", 1)[1]
    if url.startswith("git@gitlab.com:"):
        url = "https://gitlab.com/" + url.split(":", 1)[1]
    if url.endswith(".git"):
        url = url[:-4]
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url.rstrip("/") or None
    host = parsed.hostname.casefold() if parsed.hostname else parsed.netloc.casefold()
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    if host in {"www.github.com", "github.com"}:
        host = "github.com"
    if host in {"www.gitlab.com", "gitlab.com"}:
        host = "gitlab.com"
    return f"https://{host}{path}"


def package_purl(registry: str, name: str, version: str | None = None) -> str | None:
    registry = normalize_registry(registry)
    ptype = _PURL_TYPES.get(registry)
    if not ptype:
        return None
    encoded = quote(str(name).strip(), safe="/@")
    value = f"pkg:{ptype}/{encoded}"
    if version:
        value += "@" + quote(str(version).strip(), safe=".+-_")
    return value


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _extract_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://", "git+https://", "git@")):
        return value
    if isinstance(value, dict):
        for key in ("url", "html_url", "web_url", "homepage", "repository", "repository_url"):
            found = _extract_url(value.get(key))
            if found:
                return found
    return None


def _extract_repository(payload: dict[str, Any]) -> str | None:
    for key in ("repository_url", "repository", "repo_url", "source_code_uri", "source_repository", "repo"):
        value = payload.get(key)
        found = _extract_url(value)
        if found:
            return normalize_repository_url(found)
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        return _extract_repository(metadata)
    return None


def _extract_docs(payload: dict[str, Any]) -> str | None:
    for key in ("documentation_url", "documentation", "docs_url", "documentation_uri", "readme_url"):
        value = payload.get(key)
        found = _extract_url(value)
        if found:
            return found
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        return _extract_docs(metadata)
    return None


def _deps_links(payload: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for link in payload.get("links") or []:
        if not isinstance(link, dict):
            continue
        label = str(link.get("label") or link.get("type") or "").upper()
        url = str(link.get("url") or "").strip()
        if url:
            result[label] = url
    return result


def officiality_score(*, registry_repo: str | None, deps_repo: str | None, homepage: str | None, docs: str | None, version: str | None) -> tuple[int, list[str]]:
    score = 0
    evidence: list[str] = []
    if registry_repo:
        score += 40
        evidence.append("REGISTRY_REPOSITORY")
    if deps_repo:
        if registry_repo and normalize_repository_url(deps_repo) == normalize_repository_url(registry_repo):
            score += 30
            evidence.append("DEPS_DEV_REPOSITORY_MATCH")
        else:
            score += 10
            evidence.append("DEPS_DEV_REPOSITORY")
    if homepage:
        score += 5
        evidence.append("HOMEPAGE")
    if docs:
        score += 15
        evidence.append("DOCUMENTATION_URL")
    if version:
        score += 10
        evidence.append("STABLE_VERSION")
    return min(score, 100), evidence


def officiality_status(score: int) -> str:
    if score >= 80:
        return "VERIFIED_OFFICIAL"
    if score >= 55:
        return "PROBABLE_OFFICIAL"
    if score >= 30:
        return "CANDIDATE"
    return "UNVERIFIED"


def parse_linguist_languages(text: str) -> list[dict[str, Any]]:
    """Parse only the stable top-level fields needed from Linguist languages.yml.

    PyYAML is intentionally not required. Linguist keeps top-level language names at
    column zero and their scalar `type`, `group` and `language_id` fields indented.
    """
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        if not raw or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith((" ", "\t")) and raw.endswith(":"):
            if current and current.get("type") == "programming":
                records.append(current)
            current = {"name": raw[:-1].strip(), "type": None}
            continue
        if current is None:
            continue
        stripped = raw.strip()
        if stripped.startswith("type:"):
            current["type"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("group:"):
            current["group"] = stripped.split(":", 1)[1].strip().strip('"')
        elif stripped.startswith("language_id:"):
            value = stripped.split(":", 1)[1].strip()
            try:
                current["language_id"] = int(value)
            except ValueError:
                pass
    if current and current.get("type") == "programming":
        records.append(current)
    return records


class GlobalTechnologyDiscoveryEngine:
    """Build a dynamic, evidence-backed global technology catalog.

    Discovery never enables corpus ingestion by itself. Records are candidates until
    official evidence is strong enough and a later promotion step enables them.
    """

    def __init__(self, *, state_path: Path, user_agent: str, session: requests.Session | None = None):
        self.state_path = Path(state_path)
        self.user_agent = user_agent
        self.session = session or requests.Session()
        loaded = load_json(self.state_path, {})
        self.data: dict[str, Any] = loaded if isinstance(loaded, dict) else {}
        self.data.setdefault("version", 1)
        self.data.setdefault("technologies", {})
        self.data.setdefault("runs", [])

    def _headers(self, *, sparql: bool = False) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/sparql-results+json" if sparql else "application/json",
        }

    def _json(self, url: str, *, params: dict[str, Any] | None = None, sparql: bool = False) -> Any:
        response = self.session.get(url, headers=self._headers(sparql=sparql), params=params, timeout=60)
        response.raise_for_status()
        return response.json()

    def _text(self, url: str) -> str:
        response = self.session.get(url, headers={"User-Agent": self.user_agent, "Accept": "text/plain,*/*;q=0.1"}, timeout=60)
        response.raise_for_status()
        return response.text

    def _save(self) -> None:
        self.data["updated_at"] = _now()
        atomic_write_json(self.state_path, self.data)

    def _upsert(self, key: str, record: dict[str, Any]) -> dict[str, Any]:
        technologies = self.data.setdefault("technologies", {})
        previous = technologies.get(key, {}) if isinstance(technologies.get(key), dict) else {}
        first_seen = previous.get("first_seen_at") or _now()
        merged = dict(previous)
        merged.update({k: v for k, v in record.items() if v not in (None, "", [], {})})
        merged["technology_id"] = key
        merged["first_seen_at"] = first_seen
        merged["last_seen_at"] = _now()
        merged.setdefault("enabled_for_corpus", False)
        technologies[key] = merged
        return merged

    def discover_package(self, registry: str, name: str) -> dict[str, Any]:
        registry = normalize_registry(registry)
        encoded = quote(str(name).strip(), safe="")
        eco_url = f"{ECOSYSTEMS_API}/registries/{quote(registry, safe='')}/packages/{encoded}"
        package = self._json(eco_url)
        if not isinstance(package, dict):
            raise ValueError(f"unexpected ecosyste.ms payload for {registry}/{name}")

        canonical_name = str(_first(package, "name", "package_name") or name)
        latest = _first(package, "latest_release_number", "latest_version", "version")
        latest = str(latest) if latest is not None else None
        registry_repo = _extract_repository(package)
        homepage = _first(package, "homepage", "homepage_url", "project_url")
        homepage = str(homepage) if homepage else None
        docs = _extract_docs(package)

        deps_payload: dict[str, Any] = {}
        deps_repo = None
        deps_system = _DEPS_SYSTEMS.get(registry)
        if deps_system:
            deps_url = f"{DEPS_DEV_API}/systems/{deps_system}/packages/{quote(canonical_name, safe='')}"
            try:
                deps_package = self._json(deps_url)
                versions = deps_package.get("versions") or [] if isinstance(deps_package, dict) else []
                default = next((item for item in versions if isinstance(item, dict) and item.get("isDefault")), None)
                if default:
                    version_key = default.get("versionKey") or {}
                    default_version = str(version_key.get("version") or "").strip()
                    if default_version:
                        latest = default_version
                        version_url = f"{DEPS_DEV_API}/systems/{deps_system}/packages/{quote(canonical_name, safe='')}/versions/{quote(default_version, safe='')}"
                        deps_payload = self._json(version_url)
                        links = _deps_links(deps_payload if isinstance(deps_payload, dict) else {})
                        deps_repo = normalize_repository_url(links.get("SOURCE_REPO") or links.get("SOURCE") or links.get("REPOSITORY"))
                        docs = docs or links.get("DOCUMENTATION")
                        homepage = homepage or links.get("HOMEPAGE")
            except requests.RequestException:
                pass

        score, evidence = officiality_score(
            registry_repo=registry_repo,
            deps_repo=deps_repo,
            homepage=homepage,
            docs=docs,
            version=latest,
        )
        canonical_repo = registry_repo or deps_repo
        purl = package_purl(registry, canonical_name)
        key = purl or f"package:{registry}:{canonical_name.casefold()}"
        record = self._upsert(key, {
            "name": canonical_name,
            "category": "PACKAGE",
            "registry": registry,
            "purl": purl,
            "latest_stable_version": latest,
            "latest_purl": package_purl(registry, canonical_name, latest) if latest else None,
            "canonical_repository": canonical_repo,
            "documentation_url": docs,
            "official_website": homepage,
            "officiality_score": score,
            "officiality_status": officiality_status(score),
            "officiality_evidence": evidence,
            "discovery_sources": ["ecosyste.ms"] + (["deps.dev"] if deps_system else []),
            "ecosystems_url": eco_url,
        })
        self.data.setdefault("runs", []).append({"kind": "package", "registry": registry, "name": canonical_name, "at": _now()})
        self._save()
        return record

    def discover_languages(self, *, limit: int = 0) -> list[dict[str, Any]]:
        records = parse_linguist_languages(self._text(LINGUIST_LANGUAGES))
        if limit > 0:
            records = records[:limit]
        output = []
        for language in records:
            slug = re.sub(r"[^a-z0-9]+", "-", language["name"].casefold()).strip("-")
            output.append(self._upsert(f"language:{slug}", {
                "name": language["name"],
                "category": "LANGUAGE",
                "linguist_language_id": language.get("language_id"),
                "language_group": language.get("group"),
                "officiality_score": 10,
                "officiality_status": "UNVERIFIED",
                "officiality_evidence": ["GITHUB_LINGUIST_LANGUAGE"],
                "discovery_sources": ["github-linguist"],
            }))
        self.data.setdefault("runs", []).append({"kind": "languages", "count": len(output), "at": _now()})
        self._save()
        return output

    def discover_wikidata(self, *, limit: int = 500) -> list[dict[str, Any]]:
        # Q9143 = programming language; Q271680 = software framework.
        query = f"""
SELECT DISTINCT ?item ?itemLabel ?class ?website ?repository ?documentation ?version WHERE {{
  VALUES ?class {{ wd:Q9143 wd:Q271680 }}
  ?item wdt:P31/wdt:P279* ?class .
  OPTIONAL {{ ?item wdt:P856 ?website . }}
  OPTIONAL {{ ?item wdt:P1324 ?repository . }}
  OPTIONAL {{ ?item wdt:P2078 ?documentation . }}
  OPTIONAL {{ ?item wdt:P348 ?version . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language \"en,fr\". }}
}}
LIMIT {max(1, min(int(limit), 5000))}
"""
        payload = self._json(WIKIDATA_SPARQL, params={"query": query, "format": "json"}, sparql=True)
        bindings = (((payload or {}).get("results") or {}).get("bindings") or []) if isinstance(payload, dict) else []
        grouped: dict[str, dict[str, Any]] = {}
        for row in bindings:
            if not isinstance(row, dict):
                continue
            item_url = str((row.get("item") or {}).get("value") or "")
            qid = item_url.rsplit("/", 1)[-1] if item_url else ""
            if not qid:
                continue
            current = grouped.setdefault(qid, {"qid": qid, "discovery_sources": ["wikidata"]})
            label = (row.get("itemLabel") or {}).get("value")
            if label:
                current["name"] = label
            class_url = str((row.get("class") or {}).get("value") or "")
            current["category"] = "LANGUAGE" if class_url.endswith("/Q9143") else "FRAMEWORK"
            for target, source in (("official_website", "website"), ("documentation_url", "documentation"), ("latest_stable_version", "version")):
                value = (row.get(source) or {}).get("value")
                if value and not current.get(target):
                    current[target] = value
            repo = (row.get("repository") or {}).get("value")
            if repo and not current.get("canonical_repository"):
                current["canonical_repository"] = normalize_repository_url(repo)

        output = []
        for qid, item in grouped.items():
            score = 0
            evidence = ["WIKIDATA_ENTITY"]
            if item.get("canonical_repository"):
                score += 35; evidence.append("WIKIDATA_REPOSITORY")
            if item.get("official_website"):
                score += 20; evidence.append("WIKIDATA_OFFICIAL_WEBSITE")
            if item.get("documentation_url"):
                score += 25; evidence.append("WIKIDATA_DOCUMENTATION")
            if item.get("latest_stable_version"):
                score += 10; evidence.append("WIKIDATA_VERSION")
            item["officiality_score"] = min(score, 90)
            item["officiality_status"] = officiality_status(item["officiality_score"])
            item["officiality_evidence"] = evidence
            output.append(self._upsert(f"wikidata:{qid}", item))
        self.data.setdefault("runs", []).append({"kind": "wikidata", "count": len(output), "at": _now()})
        self._save()
        return output

    def audit(self) -> dict[str, Any]:
        technologies = [value for value in self.data.get("technologies", {}).values() if isinstance(value, dict)]
        by_category: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_registry: dict[str, int] = {}
        enabled = 0
        with_repo = 0
        with_docs = 0
        with_version = 0
        for item in technologies:
            category = str(item.get("category") or "UNKNOWN")
            status = str(item.get("officiality_status") or "UNVERIFIED")
            by_category[category] = by_category.get(category, 0) + 1
            by_status[status] = by_status.get(status, 0) + 1
            if item.get("registry"):
                registry = str(item["registry"])
                by_registry[registry] = by_registry.get(registry, 0) + 1
            enabled += bool(item.get("enabled_for_corpus"))
            with_repo += bool(item.get("canonical_repository"))
            with_docs += bool(item.get("documentation_url"))
            with_version += bool(item.get("latest_stable_version"))
        return {
            "catalog_path": str(self.state_path),
            "technologies": len(technologies),
            "enabled_for_corpus": enabled,
            "with_repository": with_repo,
            "with_documentation": with_docs,
            "with_version": with_version,
            "by_category": dict(sorted(by_category.items())),
            "by_officiality_status": dict(sorted(by_status.items())),
            "by_registry": dict(sorted(by_registry.items())),
            "updated_at": self.data.get("updated_at"),
        }

    def catalog(self, *, limit: int = 100, verified_only: bool = False) -> list[dict[str, Any]]:
        items: Iterable[dict[str, Any]] = (
            value for value in self.data.get("technologies", {}).values() if isinstance(value, dict)
        )
        if verified_only:
            items = (item for item in items if item.get("officiality_status") == "VERIFIED_OFFICIAL")
        ordered = sorted(items, key=lambda item: (-int(item.get("officiality_score") or 0), str(item.get("name") or "").casefold()))
        return ordered[: max(0, int(limit))] if limit else ordered
