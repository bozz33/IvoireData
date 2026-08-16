from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import quote, urlparse

import requests


def _json(session: requests.Session, url: str, user_agent: str) -> Any:
    response = session.get(url, headers={"User-Agent": user_agent, "Accept": "application/json"}, timeout=60)
    response.raise_for_status()
    return response.json()


def _repo_url(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("url") or value.get("repository") or value.get("web")
    if not isinstance(value, str) or not value.strip():
        return None
    url = value.strip()
    if url.startswith("git+"):
        url = url[4:]
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.split(":", 1)[1]
    if url.startswith("git@gitlab.com:"):
        url = "https://gitlab.com/" + url.split(":", 1)[1]
    if url.endswith(".git"):
        url = url[:-4]
    parsed = urlparse(url)
    if parsed.hostname in {"github.com", "www.github.com"}:
        return "https://github.com" + parsed.path.rstrip("/")
    if parsed.hostname in {"gitlab.com", "www.gitlab.com"}:
        return "https://gitlab.com" + parsed.path.rstrip("/")
    if parsed.scheme and parsed.netloc:
        return url.rstrip("/")
    return None


def _project_urls(info: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    urls = info.get("project_urls") or {}
    if not isinstance(urls, dict):
        urls = {}
    lowered = {str(k).casefold(): str(v) for k, v in urls.items() if v}
    repo = None
    for label in ("source", "source code", "repository", "github", "gitlab", "code"):
        if lowered.get(label):
            repo = _repo_url(lowered[label])
            if repo:
                break
    docs = None
    for label in ("documentation", "docs", "documentation site"):
        if lowered.get(label):
            docs = lowered[label]
            break
    homepage = info.get("home_page") or lowered.get("homepage") or lowered.get("home")
    return repo, str(docs) if docs else None, str(homepage) if homepage else None


def _is_prerelease(version: str) -> bool:
    value = str(version or "").strip().casefold().lstrip("v")
    if not value or value.startswith("dev-") or value.endswith("-dev"):
        return True
    return bool(re.search(r"(?:^|[.\-_])(alpha|beta|rc|pre|preview|dev|snapshot|nightly)(?:[.\-_0-9]|$)", value))


def _version_key(version: str) -> tuple[int, ...]:
    nums = [int(part) for part in re.findall(r"\d+", str(version))[:6]]
    return tuple(nums + [0] * (6 - len(nums)))


def latest_stable(versions: list[str]) -> str | None:
    stable = [str(v) for v in versions if v and not _is_prerelease(str(v))]
    return max(stable, key=_version_key) if stable else None


def _nuget_version_key(version: str) -> tuple[int, int, int, int] | None:
    """Return the NuGet numeric release tuple for a stable version.

    NuGet treats *any* hyphen suffix as prerelease, irrespective of labels such as
    alpha/beta/rc. Build metadata after '+' does not make the version prerelease and is
    ignored for precedence/normalization. NuGet supports up to four numeric release
    components and normalizes missing components with zeroes.
    """
    raw = str(version or "").strip()
    if not raw:
        return None
    core = raw.split("+", 1)[0]
    if "-" in core:
        return None
    parts = core.split(".")
    if not 1 <= len(parts) <= 4 or any(not part.isdigit() for part in parts):
        return None
    values = [int(part) for part in parts]
    values += [0] * (4 - len(values))
    return tuple(values)  # type: ignore[return-value]


def _nuget_latest_stable(entries: list[dict[str, Any]]) -> dict[str, Any]:
    stable: list[tuple[tuple[int, int, int, int], int, dict[str, Any]]] = []
    for index, entry in enumerate(entries):
        if not entry.get("listed", True):
            continue
        key = _nuget_version_key(str(entry.get("version") or ""))
        if key is not None:
            stable.append((key, index, entry))
    if not stable:
        return {}
    # Keep deterministic registry order for numerically equivalent normalized versions.
    return max(stable, key=lambda item: (item[0], item[1]))[2]


def build_purl(registry: str, name: str, version: str | None = None) -> str | None:
    registry = str(registry or "").casefold()
    type_map = {
        "npm": "npm", "npmjs.org": "npm",
        "pypi": "pypi", "pypi.org": "pypi",
        "packagist": "composer", "packagist.org": "composer",
        "cargo": "cargo", "crates.io": "cargo",
        "rubygems": "gem", "rubygems.org": "gem",
        "nuget": "nuget", "nuget.org": "nuget",
        "maven": "maven", "repo1.maven.org": "maven",
        "go": "golang", "proxy.golang.org": "golang",
        "pub": "pub", "pub.dev": "pub",
        "hex": "hex", "hex.pm": "hex",
    }
    ptype = type_map.get(registry)
    if not ptype:
        return None
    raw = str(name).strip()
    if ptype == "npm" and raw.startswith("@") and "/" in raw:
        scope, package = raw.split("/", 1)
        encoded_name = quote(scope, safe="") + "/" + quote(package, safe="")
    elif ptype == "maven" and ":" in raw:
        group, artifact = raw.split(":", 1)
        encoded_name = quote(group, safe=".") + "/" + quote(artifact, safe=".")
    else:
        encoded_name = "/".join(quote(part, safe="._-") for part in raw.split("/"))
    value = f"pkg:{ptype}/{encoded_name}"
    if version:
        value += "@" + quote(str(version).strip(), safe=".+-_")
    return value


def _npm(session: requests.Session, name: str, user_agent: str) -> dict[str, Any]:
    url = "https://registry.npmjs.org/" + quote(name, safe="")
    payload = _json(session, url, user_agent)
    latest = str((payload.get("dist-tags") or {}).get("latest") or "").strip() or None
    current = ((payload.get("versions") or {}).get(latest) or {}) if latest else {}
    repo = _repo_url(current.get("repository") or payload.get("repository"))
    homepage = current.get("homepage") or payload.get("homepage")
    return {
        "authority_source": "npm",
        "native_registry_url": url,
        "name": str(payload.get("name") or name),
        "latest_stable_version": latest,
        "canonical_repository": repo,
        "official_website": str(homepage) if homepage else None,
    }


def _pypi(session: requests.Session, name: str, user_agent: str) -> dict[str, Any]:
    url = "https://pypi.org/pypi/" + quote(name, safe="") + "/json"
    payload = _json(session, url, user_agent)
    info = payload.get("info") or {}
    repo, docs, homepage = _project_urls(info)
    return {
        "authority_source": "pypi",
        "native_registry_url": url,
        "name": str(info.get("name") or name),
        "latest_stable_version": str(info.get("version") or "").strip() or None,
        "canonical_repository": repo,
        "documentation_url": docs,
        "official_website": homepage,
    }


def _packagist(session: requests.Session, name: str, user_agent: str) -> dict[str, Any]:
    url = "https://packagist.org/packages/" + name + ".json"
    payload = _json(session, url, user_agent)
    package = payload.get("package") or {}
    versions = package.get("versions") or {}
    latest = latest_stable(list(versions.keys())) if isinstance(versions, dict) else None
    current = versions.get(latest) or {} if latest and isinstance(versions, dict) else {}
    downloads = package.get("downloads") or {}
    return {
        "authority_source": "packagist",
        "native_registry_url": url,
        "name": str(package.get("name") or name),
        "latest_stable_version": latest,
        "canonical_repository": _repo_url(package.get("repository") or current.get("source")),
        "official_website": current.get("homepage"),
        "downloads_total": int(downloads.get("total") or 0),
        "downloads_recent": int(downloads.get("monthly") or 0),
        "dependents_count": int(package.get("dependents") or 0),
        "favorites_count": int(package.get("favers") or 0),
    }


def _crates(session: requests.Session, name: str, user_agent: str) -> dict[str, Any]:
    url = "https://crates.io/api/v1/crates/" + quote(name, safe="")
    payload = _json(session, url, user_agent)
    crate = payload.get("crate") or {}
    return {
        "authority_source": "crates.io",
        "native_registry_url": url,
        "name": str(crate.get("name") or crate.get("id") or name),
        "latest_stable_version": crate.get("max_stable_version") or crate.get("max_version"),
        "canonical_repository": _repo_url(crate.get("repository")),
        "documentation_url": crate.get("documentation"),
        "official_website": crate.get("homepage"),
        "downloads_total": int(crate.get("downloads") or 0),
        "downloads_recent": int(crate.get("recent_downloads") or 0),
    }


def _rubygems(session: requests.Session, name: str, user_agent: str) -> dict[str, Any]:
    url = "https://rubygems.org/api/v1/gems/" + quote(name, safe="") + ".json"
    payload = _json(session, url, user_agent)
    return {
        "authority_source": "rubygems",
        "native_registry_url": url,
        "name": str(payload.get("name") or name),
        "latest_stable_version": str(payload.get("version") or "").strip() or None,
        "canonical_repository": _repo_url(payload.get("source_code_uri")),
        "documentation_url": payload.get("documentation_uri"),
        "official_website": payload.get("homepage_uri"),
        "downloads_total": int(payload.get("downloads") or 0),
        "downloads_recent": int(payload.get("version_downloads") or 0),
    }


def _nuget_registration_base(session: requests.Session, user_agent: str) -> str:
    index = _json(session, "https://api.nuget.org/v3/index.json", user_agent)
    resources = index.get("resources") or []
    preferred = None
    fallback = None
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        rtype = str(resource.get("@type") or "")
        rid = str(resource.get("@id") or "")
        if not rid:
            continue
        if rtype == "RegistrationsBaseUrl/3.6.0":
            preferred = rid
            break
        if rtype.startswith("RegistrationsBaseUrl"):
            fallback = fallback or rid
    base = preferred or fallback
    if not base:
        raise ValueError("NuGet service index has no RegistrationsBaseUrl")
    return base.rstrip("/") + "/"


def _nuget(session: requests.Session, name: str, user_agent: str) -> dict[str, Any]:
    base = _nuget_registration_base(session, user_agent)
    url = base + quote(name.casefold(), safe="") + "/index.json"
    index = _json(session, url, user_agent)
    leaves: list[dict[str, Any]] = []
    for page in index.get("items") or []:
        if not isinstance(page, dict):
            continue
        items = page.get("items")
        if items is None and page.get("@id"):
            page = _json(session, str(page["@id"]), user_agent)
            items = page.get("items") or []
        for leaf in items or []:
            if isinstance(leaf, dict):
                entry = leaf.get("catalogEntry")
                if isinstance(entry, dict):
                    leaves.append(entry)
    current = _nuget_latest_stable(leaves)
    if not current:
        current = leaves[-1] if leaves else {}
    project = current.get("projectUrl")
    repo = _repo_url(current.get("repository"))
    return {
        "authority_source": "nuget",
        "native_registry_url": url,
        "name": str(current.get("id") or name),
        "latest_stable_version": str(current.get("version") or "").strip() or None,
        "canonical_repository": repo,
        "documentation_url": current.get("readmeUrl"),
        "official_website": str(project) if project else None,
    }


_ADAPTERS = {
    "npmjs.org": _npm,
    "pypi.org": _pypi,
    "packagist.org": _packagist,
    "crates.io": _crates,
    "rubygems.org": _rubygems,
    "nuget.org": _nuget,
}


def native_package_metadata(registry: str, name: str, *, session: requests.Session, user_agent: str) -> dict[str, Any] | None:
    adapter = _ADAPTERS.get(str(registry or "").casefold())
    if adapter is None:
        return None
    return adapter(session, name, user_agent)


def importance_score(record: dict[str, Any]) -> tuple[int, str]:
    """Heuristic importance score for ranking discovery, never an officiality proof."""
    downloads = max(0, int(record.get("downloads_total") or record.get("downloads") or 0))
    recent = max(0, int(record.get("downloads_recent") or record.get("recent_downloads") or 0))
    dependents = max(0, int(record.get("dependents_count") or record.get("dependent_repos_count") or 0))
    stars = max(0, int(record.get("repository_stars") or record.get("stars") or record.get("favorites_count") or 0))
    officiality = max(0, min(100, int(record.get("officiality_score") or 0)))

    score = 0.0
    if downloads:
        score += min(35.0, math.log10(downloads + 1) * 5.0)
    if recent:
        score += min(15.0, math.log10(recent + 1) * 2.5)
    if dependents:
        score += min(25.0, math.log10(dependents + 1) * 6.0)
    if stars:
        score += min(15.0, math.log10(stars + 1) * 3.0)
    score += officiality * 0.10
    value = min(100, int(round(score)))
    tier = "CORE" if value >= 80 else "MAJOR" if value >= 60 else "SIGNIFICANT" if value >= 40 else "LONG_TAIL"
    return value, tier
