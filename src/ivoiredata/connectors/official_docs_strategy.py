from __future__ import annotations

from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlparse

from ..state_io import atomic_write_json
from . import official_docs as base
from .official_git_docs import official_git_docs_resource, parse_github_tree_url

_original_official_docs_resource = base.official_docs_resource


class _GitLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() != "a":
            return
        values = dict(attrs)
        href = str(values.get("href") or "").strip()
        if href:
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = None
            self._text = []


def _github_candidate(url: str, anchor_text: str = "") -> dict[str, Any] | None:
    parsed = urlparse(url)
    if (parsed.hostname or "").casefold() not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if repo.casefold() in {"issues", "pulls", "discussions"}:
        return None

    action = parts[2] if len(parts) > 2 else ""
    ref = parts[3] if len(parts) > 3 and action in {"tree", "blob", "edit"} else None
    path = "/".join(parts[4:]) if ref else ""
    score = 15
    if action == "edit":
        score += 85
    elif action == "blob":
        score += 75
    elif action == "tree":
        score += 65

    label = anchor_text.casefold()
    if any(token in label for token in ("edit this page", "edit page", "source", "github", "repository", "repo")):
        score += 25
    if any(token in path.casefold() for token in ("docs/", "documentation/", "guide/", "manual/", "reference/")):
        score += 15

    return {
        "repository": f"{owner}/{repo}",
        "ref": ref,
        "path": path,
        "action": action or "repository",
        "score": score,
        "url": url,
        "anchor_text": anchor_text,
    }


def _prefix_from_candidate(candidate: dict[str, Any]) -> str | None:
    path = str(candidate.get("path") or "").strip("/")
    if not path:
        return None
    action = str(candidate.get("action") or "")
    if action == "tree":
        return path.rstrip("/") + "/"
    parent = str(PurePosixPath(path).parent)
    if parent and parent != ".":
        return parent.rstrip("/") + "/"
    return None


def discover_official_git_source(url: str, user_agent: str) -> dict[str, Any] | None:
    """Discover a strong canonical GitHub documentation link from an official page.

    Only high-confidence source/edit links are accepted automatically. A plain GitHub
    link in a footer is intentionally not enough: ambiguity falls back to the existing
    llms/sitemap/HTML connector instead of guessing a third-party repository.
    """
    import requests

    response = requests.get(
        url,
        headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"},
        timeout=30,
        allow_redirects=True,
    )
    response.raise_for_status()
    ctype = (response.headers.get("content-type") or "").casefold()
    if "html" not in ctype and not response.content.lstrip().startswith(b"<"):
        return None

    parser = _GitLinkParser()
    parser.feed(response.text)
    candidates: list[dict[str, Any]] = []
    for href, text in parser.links:
        absolute = urljoin(response.url, href)
        candidate = _github_candidate(absolute, text)
        if candidate is not None:
            candidates.append(candidate)
    if not candidates:
        return None

    candidates.sort(key=lambda item: (int(item["score"]), bool(item.get("ref")), len(str(item.get("path") or ""))), reverse=True)
    winner = candidates[0]
    if int(winner["score"]) < 75 or not winner.get("ref"):
        return None
    winner = dict(winner)
    winner["include_prefix"] = _prefix_from_candidate(winner)
    return winner


def _write_strategy(snapshot_dir, payload: dict[str, Any]) -> None:
    if snapshot_dir is not None:
        atomic_write_json(snapshot_dir / "strategy_resolution.json", payload)


def _git_resource(*, source_id: str, repository: str, ref: str, kwargs: dict[str, Any], include_prefixes=None):
    return official_git_docs_resource(
        source_id=source_id,
        repository=repository,
        ref=ref,
        user_agent=kwargs.get("user_agent", "IvoireData/0.8.3"),
        include_prefixes=include_prefixes if include_prefixes is not None else kwargs.get("include_prefixes", ()),
        exclude_patterns=kwargs.get("exclude_patterns", ()),
        max_pages=kwargs.get("max_pages", 100_000),
        max_bytes_per_page=kwargs.get("max_bytes_per_page", 12_000_000),
        max_new_bytes_per_run=kwargs.get("max_new_bytes_per_run", 500_000_000),
        request_pause_seconds=kwargs.get("request_pause_seconds", 0.0),
        snapshot_dir=kwargs.get("snapshot_dir"),
        metadata_base=kwargs.get("metadata_base"),
        upstream_state_path=kwargs.get("upstream_state_path"),
        license_name=kwargs.get("license_name"),
        license_url=kwargs.get("license_url"),
        training_eligible=kwargs.get("training_eligible", False),
        license_review_status=kwargs.get("license_review_status", "UNREVIEWED"),
    )


def official_docs_resource(*, source_id: str, url: str, **kwargs):
    parsed = parse_github_tree_url(url)
    snapshot_dir = kwargs.get("snapshot_dir")
    metadata = dict(kwargs.get("metadata_base") or {})
    strategy_mode = str(metadata.get("source_strategy") or "AUTO").upper()

    if parsed is not None:
        repository, ref = parsed
        _write_strategy(snapshot_dir, {
            "source_id": source_id,
            "strategy": "OFFICIAL_GIT",
            "reason": "CANONICAL_GIT_URL",
            "repository": repository,
            "ref": ref,
            "official_url": metadata.get("public_docs_url") or url,
        })
        return _git_resource(source_id=source_id, repository=repository, ref=ref, kwargs=kwargs)

    if strategy_mode not in {"WEB", "OFFICIAL_WEB", "FORCE_WEB"}:
        try:
            discovered = discover_official_git_source(url, kwargs.get("user_agent", "IvoireData/0.8.3"))
        except Exception as exc:
            discovered = None
            _write_strategy(snapshot_dir, {
                "source_id": source_id,
                "strategy": "OFFICIAL_WEB",
                "reason": "GIT_AUTO_DISCOVERY_FAILED",
                "official_url": url,
                "error": str(exc)[:1000],
            })
        if discovered is not None:
            prefix = discovered.get("include_prefix")
            configured = [str(value) for value in kwargs.get("include_prefixes", ()) if str(value)]
            filesystem_prefixes = [value for value in configured if not value.startswith("/")]
            prefixes = [str(prefix)] if prefix else filesystem_prefixes
            metadata.update({
                "source_strategy": "OFFICIAL_GIT_AUTO",
                "public_docs_url": url,
                "canonical_repository": discovered["repository"],
                "canonical_git_ref": discovered["ref"],
            })
            kwargs["metadata_base"] = metadata
            _write_strategy(snapshot_dir, {
                "source_id": source_id,
                "strategy": "OFFICIAL_GIT",
                "reason": "DISCOVERED_FROM_OFFICIAL_PAGE",
                "official_url": url,
                **discovered,
                "effective_include_prefixes": prefixes,
            })
            return _git_resource(
                source_id=source_id,
                repository=str(discovered["repository"]),
                ref=str(discovered["ref"]),
                kwargs=kwargs,
                include_prefixes=prefixes,
            )

    _write_strategy(snapshot_dir, {
        "source_id": source_id,
        "strategy": "OFFICIAL_WEB",
        "reason": "NO_HIGH_CONFIDENCE_CANONICAL_GIT_SOURCE",
        "official_url": url,
    })
    return _original_official_docs_resource(source_id=source_id, url=url, **kwargs)


# Engine imports official_docs_resource from the base module only after package __init__
# runs. Patch the exported function once so existing connector names/config remain stable.
base.official_docs_resource = official_docs_resource
