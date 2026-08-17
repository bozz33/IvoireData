from __future__ import annotations

import base64
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests

from .technology_discovery import normalize_registry, normalize_repository_url
from .technology_documentation import canonical_documentation_url
from .technology_harvester import TechnologyHarvestQueue
from .technology_qualification_v2 import _is_registry_landing_url


DOCUMENTATION_DISCOVERY_SCHEMA_VERSION = 1
DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_RETRY_BASE_SECONDS = 6 * 60 * 60
DEFAULT_RETRY_MAX_SECONDS = 7 * 24 * 60 * 60
DISCOVERY_ACCEPT_SCORE = 85
MAX_HTML_BYTES = 2_000_000
MAX_README_BYTES = 2_000_000

_DOC_TERMS = (
    "docs",
    "documentation",
    "developer",
    "developers",
    "guide",
    "guides",
    "manual",
    "reference",
    "api",
    "learn",
    "tutorial",
    "handbook",
)
_DOC_DIR_NAMES = {
    "doc",
    "docs",
    "documentation",
    "guide",
    "guides",
    "manual",
    "reference",
}
_LOW_VALUE_HOSTS = {
    "central.sonatype.com",
    "search.maven.org",
    "repo1.maven.org",
    "repo.maven.apache.org",
    "www.npmjs.com",
    "npmjs.com",
    "pypi.org",
    "www.nuget.org",
    "nuget.org",
    "crates.io",
}
_SOCIAL_HOSTS = {
    "twitter.com",
    "x.com",
    "facebook.com",
    "linkedin.com",
    "discord.com",
    "discord.gg",
    "youtube.com",
    "www.youtube.com",
}

HostResolver = Callable[[str], list[str]]


def _now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime | None = None) -> str:
    return (value or _now_dt()).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _default_host_resolver(host: str) -> list[str]:
    addresses: list[str] = []
    for row in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM):
        value = str(row[4][0])
        if value not in addresses:
            addresses.append(value)
    return addresses


def _is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _doc_signal(value: str) -> bool:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold())
    tokens = set(text.split())
    return any(term in tokens for term in _DOC_TERMS)


def _host(value: str | None) -> str:
    try:
        return (urlparse(str(value or "")).hostname or "").casefold()
    except ValueError:
        return ""


def _related_hosts(left: str | None, right: str | None) -> bool:
    a = _host(left)
    b = _host(right)
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _github_slug(repository: str | None) -> tuple[str, str] | None:
    normalized = normalize_repository_url(repository)
    if not normalized:
        return None
    parsed = urlparse(normalized)
    if (parsed.hostname or "").casefold() != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def _safe_candidate_url(value: Any) -> str | None:
    canonical = canonical_documentation_url(value)
    if not canonical:
        return None
    host = _host(canonical)
    if not host or host in _LOW_VALUE_HOSTS or host in _SOCIAL_HOSTS:
        return None
    return canonical


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._href: str | None = None
        self._parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs):
        lowered = tag.casefold()
        if lowered in {"script", "style", "noscript"}:
            self._skip += 1
        if lowered == "a" and self._skip == 0:
            self._href = next(
                (str(value) for key, value in attrs if key.casefold() == "href" and value),
                None,
            )
            self._parts = []

    def handle_endtag(self, tag: str):
        lowered = tag.casefold()
        if lowered == "a" and self._href:
            self.links.append((self._href, " ".join(self._parts).strip()))
            self._href = None
            self._parts = []
        if lowered in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str):
        if self._href and self._skip == 0:
            self._parts.append(data)


@dataclass(frozen=True)
class Candidate:
    url: str
    score: int
    kind: str
    evidence: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "score": self.score,
            "kind": self.kind,
            "evidence": list(self.evidence),
        }


class ActiveDocumentationDiscovery:
    """Find canonical official documentation roots for verified packages.

    Discovery is intentionally separate from the documentation crawler. It only
    examines high-trust surfaces tied to an already VERIFIED package authority:

    * canonical GitHub repository metadata and its root contents;
    * links in the canonical repository README;
    * the verified project website and its documentation links;
    * ``llms.txt`` published by that verified website.

    Package-registry landing pages (notably Maven Central) are explicitly rejected as
    documentation targets. Only candidates scoring at least ``DISCOVERY_ACCEPT_SCORE``
    are promoted back to ``documentation_targets`` for the existing ``official_docs``
    connector.
    """

    def __init__(
        self,
        *,
        queue: TechnologyHarvestQueue,
        user_agent: str,
        session: requests.Session | None = None,
        host_resolver: HostResolver | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_base_seconds: int = DEFAULT_RETRY_BASE_SECONDS,
        retry_max_seconds: int = DEFAULT_RETRY_MAX_SECONDS,
    ):
        self.queue = queue
        self.db = queue.db
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self.host_resolver = host_resolver or _default_host_resolver
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_seconds = max(1, int(retry_base_seconds))
        self.retry_max_seconds = max(self.retry_base_seconds, int(retry_max_seconds))
        self.network_requests = 0
        self._init_schema()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS documentation_discovery_results (
                registry TEXT NOT NULL,
                name TEXT NOT NULL,
                source_id TEXT NOT NULL,
                input_target_url TEXT,
                input_target_status TEXT NOT NULL,
                target_resolved_at TEXT NOT NULL,
                authority_checked_at TEXT NOT NULL,
                discovery_status TEXT NOT NULL,
                selected_url TEXT,
                selected_kind TEXT,
                selected_score INTEGER NOT NULL DEFAULT 0,
                candidates_json TEXT NOT NULL DEFAULT '[]',
                evidence_json TEXT NOT NULL DEFAULT '[]',
                errors_json TEXT NOT NULL DEFAULT '[]',
                attempts INTEGER NOT NULL DEFAULT 0,
                first_checked_at TEXT NOT NULL,
                last_checked_at TEXT NOT NULL,
                next_retry_at TEXT,
                PRIMARY KEY (registry,name)
            );
            CREATE INDEX IF NOT EXISTS idx_docs_discovery_status_retry
                ON documentation_discovery_results(discovery_status,next_retry_at,attempts);
            CREATE TABLE IF NOT EXISTS documentation_discovery_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.db.execute(
            """
            INSERT INTO documentation_discovery_meta(key,value,updated_at)
            VALUES('schema_version',?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (str(DOCUMENTATION_DISCOVERY_SCHEMA_VERSION), _iso()),
        )
        self.db.commit()

    def _guard_public_url(self, value: str) -> str:
        canonical = canonical_documentation_url(value)
        if not canonical:
            raise ValueError(f"invalid discovery URL: {value!r}")
        parsed = urlparse(canonical)
        host = (parsed.hostname or "").casefold()
        if not host or host == "localhost" or host.endswith(".local"):
            raise ValueError(f"non-public discovery host rejected: {host!r}")
        try:
            direct = ipaddress.ip_address(host)
        except ValueError:
            direct = None
        if direct is not None:
            if not _is_public_ip(host):
                raise ValueError(f"non-public discovery address rejected: {host}")
            return canonical
        addresses = self.host_resolver(host)
        if not addresses or any(not _is_public_ip(address) for address in addresses):
            raise ValueError(f"host did not resolve exclusively to public addresses: {host}")
        return canonical

    def _get(
        self,
        url: str,
        *,
        accept: str,
        cap: int = MAX_HTML_BYTES,
        trusted_github_api: bool = False,
    ):
        request_url = url if trusted_github_api else self._guard_public_url(url)
        self.network_requests += 1
        response = self.session.get(
            request_url,
            headers={"User-Agent": self.user_agent, "Accept": accept},
            timeout=30,
            allow_redirects=True,
        )
        response.raise_for_status()
        if not trusted_github_api:
            self._guard_public_url(str(response.url or request_url))
        try:
            declared = int(response.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared and declared > cap:
            raise ValueError(f"discovery response too large: {declared}>{cap} {request_url}")
        raw = bytes(response.content or b"")
        if len(raw) > cap:
            raise ValueError(f"discovery response too large: {len(raw)}>{cap} {request_url}")
        return response, raw

    @staticmethod
    def _add_candidate(bucket: dict[str, Candidate], candidate: Candidate) -> None:
        previous = bucket.get(candidate.url)
        if previous is None or candidate.score > previous.score:
            bucket[candidate.url] = candidate
        elif previous.score == candidate.score:
            evidence = tuple(dict.fromkeys((*previous.evidence, *candidate.evidence)))
            bucket[candidate.url] = Candidate(
                candidate.url,
                candidate.score,
                previous.kind,
                evidence,
            )

    def _repository_candidates(
        self,
        repository: str | None,
        bucket: dict[str, Candidate],
        errors: list[str],
    ) -> str | None:
        slug = _github_slug(repository)
        if slug is None:
            return None
        owner, repo = slug
        api = f"https://api.github.com/repos/{owner}/{repo}"
        try:
            response, raw = self._get(
                api,
                accept="application/vnd.github+json",
                cap=MAX_README_BYTES,
                trusted_github_api=True,
            )
            metadata = response.json()
            if not isinstance(metadata, dict):
                metadata = json.loads(raw.decode("utf-8", "replace"))
            canonical_repo = normalize_repository_url(metadata.get("html_url")) or normalize_repository_url(repository)
            default_branch = str(metadata.get("default_branch") or "main")
            homepage = _safe_candidate_url(metadata.get("homepage"))
            if homepage:
                score = 94 if _doc_signal(homepage) else 90
                self._add_candidate(
                    bucket,
                    Candidate(
                        homepage,
                        score,
                        "CANONICAL_REPOSITORY_HOMEPAGE",
                        ("VERIFIED_REPOSITORY_DECLARED_HOMEPAGE",),
                    ),
                )
        except Exception as exc:
            errors.append(f"github repository metadata: {str(exc)[:500]}")
            canonical_repo = normalize_repository_url(repository)
            default_branch = "main"

        try:
            contents_api = f"{api}/contents"
            response, raw = self._get(
                contents_api,
                accept="application/vnd.github+json",
                cap=MAX_README_BYTES,
                trusted_github_api=True,
            )
            payload = response.json()
            if not isinstance(payload, list):
                payload = json.loads(raw.decode("utf-8", "replace"))
            for item in payload if isinstance(payload, list) else []:
                if not isinstance(item, dict) or str(item.get("type") or "") != "dir":
                    continue
                name = str(item.get("name") or "").casefold()
                if name not in _DOC_DIR_NAMES:
                    continue
                candidate_url = _safe_candidate_url(item.get("html_url"))
                if not candidate_url and canonical_repo:
                    candidate_url = canonical_documentation_url(
                        f"{canonical_repo}/tree/{default_branch}/{item.get('path') or name}"
                    )
                if candidate_url:
                    self._add_candidate(
                        bucket,
                        Candidate(
                            candidate_url,
                            100,
                            "CANONICAL_REPOSITORY_DOCS_DIRECTORY",
                            ("VERIFIED_REPOSITORY_DOCS_DIRECTORY",),
                        ),
                    )
        except Exception as exc:
            errors.append(f"github repository contents: {str(exc)[:500]}")

        try:
            readme_api = f"{api}/readme"
            response, raw = self._get(
                readme_api,
                accept="application/vnd.github+json",
                cap=MAX_README_BYTES,
                trusted_github_api=True,
            )
            payload = response.json()
            if not isinstance(payload, dict):
                payload = json.loads(raw.decode("utf-8", "replace"))
            encoded = str(payload.get("content") or "")
            if encoded:
                markdown = base64.b64decode(encoded, validate=False).decode("utf-8", "replace")
                base = canonical_repo or str(repository or "")
                for label, link in re.findall(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+[^)]*)?\)", markdown):
                    candidate_url = _safe_candidate_url(urljoin(base + "/", link.strip("<>")))
                    if not candidate_url:
                        continue
                    if not (_doc_signal(label) or _doc_signal(candidate_url)):
                        continue
                    self._add_candidate(
                        bucket,
                        Candidate(
                            candidate_url,
                            97,
                            "CANONICAL_REPOSITORY_README_DOCS_LINK",
                            ("VERIFIED_REPOSITORY_README_DOCS_LINK",),
                        ),
                    )
        except Exception as exc:
            # A missing README is not a fatal discovery failure.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status != 404:
                errors.append(f"github repository readme: {str(exc)[:500]}")
        return canonical_repo

    def _website_candidates(
        self,
        website: str | None,
        bucket: dict[str, Candidate],
        errors: list[str],
    ) -> None:
        website = _safe_candidate_url(website)
        if not website:
            return
        try:
            response, raw = self._get(
                website,
                accept="text/html,application/xhtml+xml,text/plain,*/*;q=0.1",
            )
            final_url = _safe_candidate_url(response.url) or website
            if final_url:
                self._add_candidate(
                    bucket,
                    Candidate(
                        final_url,
                        92 if _doc_signal(final_url) else 86,
                        "VERIFIED_OFFICIAL_WEBSITE_ROOT",
                        ("VERIFIED_PROJECT_WEBSITE",),
                    ),
                )
            ctype = str(response.headers.get("content-type") or "").casefold()
            if "html" in ctype or raw.lstrip().startswith(b"<"):
                parser = _AnchorParser()
                parser.feed(raw.decode("utf-8", "replace"))
                for href, label in parser.links:
                    candidate_url = _safe_candidate_url(urljoin(str(response.url), href))
                    if not candidate_url:
                        continue
                    signal = _doc_signal(label) or _doc_signal(candidate_url)
                    if not signal:
                        continue
                    related = _related_hosts(candidate_url, final_url)
                    self._add_candidate(
                        bucket,
                        Candidate(
                            candidate_url,
                            96 if related else 94,
                            "OFFICIAL_WEBSITE_DOCS_LINK",
                            (
                                "OFFICIAL_WEBSITE_LINKS_DOCUMENTATION",
                                "RELATED_DOCUMENTATION_HOST" if related else "EXTERNAL_DOCS_LINK_FROM_OFFICIAL_WEBSITE",
                            ),
                        ),
                    )
        except Exception as exc:
            errors.append(f"official website: {str(exc)[:500]}")

        parsed = urlparse(website)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        llms_urls = list(dict.fromkeys([urljoin(website.rstrip("/") + "/", "llms.txt"), origin + "/llms.txt"]))
        for llms_url in llms_urls[:2]:
            try:
                response, raw = self._get(
                    llms_url,
                    accept="text/plain,text/markdown,*/*;q=0.1",
                    cap=MAX_README_BYTES,
                )
                text = raw.decode("utf-8", "replace")
                links = [match[1] for match in re.findall(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+[^)]*)?\)", text)]
                links.extend(
                    line.strip().split()[0]
                    for line in text.splitlines()
                    if line.strip().startswith(("http://", "https://"))
                )
                for link in links:
                    candidate_url = _safe_candidate_url(urljoin(str(response.url), link.strip("<>")))
                    if not candidate_url or not _doc_signal(candidate_url):
                        continue
                    self._add_candidate(
                        bucket,
                        Candidate(
                            candidate_url,
                            98,
                            "OFFICIAL_WEBSITE_LLMS_DOCS_LINK",
                            ("OFFICIAL_WEBSITE_LLMS_TXT_DOCUMENTATION",),
                        ),
                    )
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status not in {404, 410}:
                    errors.append(f"llms.txt {llms_url}: {str(exc)[:300]}")

    def _validate_selected(self, candidate: Candidate) -> Candidate:
        # Validate the final target once before promotion, including redirect safety.
        response, _ = self._get(
            candidate.url,
            accept="text/html,text/markdown,text/plain,application/json,*/*;q=0.1",
            cap=MAX_HTML_BYTES,
        )
        final = _safe_candidate_url(response.url) or candidate.url
        if not final:
            raise ValueError("selected documentation target redirected to a rejected host")
        if final == candidate.url:
            return candidate
        return Candidate(
            final,
            candidate.score,
            candidate.kind,
            tuple(dict.fromkeys((*candidate.evidence, "TARGET_REDIRECT_CANONICALIZED"))),
        )

    def _eligible(self, *, limit: int, registry: str | None) -> list[dict[str, Any]]:
        if int(limit) <= 0:
            raise ValueError("active documentation discovery is intentionally bounded; --limit must be > 0")
        normalized = normalize_registry(registry) if registry else None
        now = _iso()
        where = [
            "a.authority_status='AUTHORITY_VERIFIED'",
            "(d.target_status IN ('DOCS_DISCOVERY_REQUIRED','DOCS_TARGET_MISSING') "
            "OR d.target_url LIKE 'https://central.sonatype.com/%' "
            "OR d.target_url LIKE 'http://central.sonatype.com/%' "
            "OR d.target_url LIKE 'https://search.maven.org/%')",
            "(x.registry IS NULL OR d.last_resolved_at>x.target_resolved_at "
            "OR (x.discovery_status IN ('RETRY','NO_MATCH') AND x.attempts<? "
            "AND (x.next_retry_at IS NULL OR x.next_retry_at<=?)))",
        ]
        params: list[Any] = [self.max_attempts, now]
        if normalized:
            where.append("d.registry=?")
            params.append(normalized)
        params.append(int(limit))
        rows = self.db.execute(
            """
            SELECT d.*,a.last_checked_at AS authority_last_checked_at,
                   a.documentation_url AS authority_documentation_url,
                   a.official_website AS authority_official_website,
                   a.canonical_repository AS authority_repository,
                   a.attempts AS authority_attempts_live
            FROM documentation_targets AS d
            JOIN authority_results AS a ON a.registry=d.registry AND a.name=d.name
            LEFT JOIN documentation_discovery_results AS x
              ON x.registry=d.registry AND x.name=d.name
            WHERE """
            + " AND ".join(where)
            + " ORDER BY d.programming_language ASC,d.registry ASC,d.name ASC LIMIT ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def _promote_target(self, row: dict[str, Any], candidate: Candidate) -> None:
        evidence = []
        try:
            evidence = json.loads(str(row.get("evidence_json") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            evidence = []
        if not isinstance(evidence, list):
            evidence = []
        evidence = list(dict.fromkeys([*(str(item) for item in evidence), "ACTIVE_DOCS_DISCOVERY", *candidate.evidence]))
        with self.db:
            self.db.execute(
                """
                UPDATE documentation_targets
                SET target_url=?,target_kind=?,source_strategy='AUTO',
                    target_status='READY_FOR_DOCS_CONNECTOR',
                    target_confidence='ACTIVE_DISCOVERY_VERIFIED',
                    evidence_json=?,last_resolved_at=?
                WHERE registry=? AND name=?
                """,
                (
                    candidate.url,
                    candidate.kind,
                    _json(evidence),
                    _iso(),
                    row["registry"],
                    row["name"],
                ),
            )

    def _demote_weak_registry_target(self, row: dict[str, Any]) -> None:
        if not _is_registry_landing_url(row.get("target_url")):
            return
        evidence = []
        try:
            evidence = json.loads(str(row.get("evidence_json") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            evidence = []
        if not isinstance(evidence, list):
            evidence = []
        evidence = list(dict.fromkeys([*(str(item) for item in evidence), "REGISTRY_LANDING_REJECTED_AS_DOCUMENTATION"]))
        with self.db:
            self.db.execute(
                """
                UPDATE documentation_targets
                SET target_status='DOCS_DISCOVERY_REQUIRED',
                    target_kind='REGISTRY_LANDING_REJECTED',
                    target_confidence='REJECTED_REGISTRY_LANDING',
                    evidence_json=?,last_resolved_at=?
                WHERE registry=? AND name=?
                """,
                (_json(evidence), _iso(), row["registry"], row["name"]),
            )

    def _save_discovery(
        self,
        row: dict[str, Any],
        *,
        status: str,
        candidates: list[Candidate],
        selected: Candidate | None,
        errors: list[str],
    ) -> dict[str, Any]:
        previous = self.db.execute(
            "SELECT attempts,first_checked_at FROM documentation_discovery_results WHERE registry=? AND name=?",
            (row["registry"], row["name"]),
        ).fetchone()
        attempts = int(previous["attempts"] if previous else 0) + 1
        now = _iso()
        next_retry = None
        if status in {"RETRY", "NO_MATCH"}:
            if attempts >= self.max_attempts:
                status = "REVIEW_EXHAUSTED"
            else:
                base = self.retry_base_seconds if status == "RETRY" else max(self.retry_base_seconds, 24 * 60 * 60)
                delay = min(self.retry_max_seconds, base * (2 ** min(attempts - 1, 8)))
                next_retry = _iso(_now_dt() + timedelta(seconds=delay))
        evidence = list(selected.evidence) if selected else []
        with self.db:
            self.db.execute(
                """
                INSERT INTO documentation_discovery_results(
                    registry,name,source_id,input_target_url,input_target_status,target_resolved_at,
                    authority_checked_at,discovery_status,selected_url,selected_kind,selected_score,
                    candidates_json,evidence_json,errors_json,attempts,first_checked_at,last_checked_at,next_retry_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(registry,name) DO UPDATE SET
                    source_id=excluded.source_id,
                    input_target_url=excluded.input_target_url,
                    input_target_status=excluded.input_target_status,
                    target_resolved_at=excluded.target_resolved_at,
                    authority_checked_at=excluded.authority_checked_at,
                    discovery_status=excluded.discovery_status,
                    selected_url=excluded.selected_url,
                    selected_kind=excluded.selected_kind,
                    selected_score=excluded.selected_score,
                    candidates_json=excluded.candidates_json,
                    evidence_json=excluded.evidence_json,
                    errors_json=excluded.errors_json,
                    attempts=excluded.attempts,
                    last_checked_at=excluded.last_checked_at,
                    next_retry_at=excluded.next_retry_at
                """,
                (
                    row["registry"],
                    row["name"],
                    row["source_id"],
                    row.get("target_url"),
                    row.get("target_status") or "UNKNOWN",
                    row["last_resolved_at"],
                    row.get("authority_last_checked_at") or row.get("authority_checked_at") or "",
                    status,
                    selected.url if selected else None,
                    selected.kind if selected else None,
                    int(selected.score if selected else 0),
                    _json([candidate.as_dict() for candidate in candidates[:100]]),
                    _json(evidence),
                    _json(errors[:50]),
                    attempts,
                    str(previous["first_checked_at"]) if previous else now,
                    now,
                    next_retry,
                ),
            )
        return {
            "registry": row["registry"],
            "name": row.get("canonical_name") or row["name"],
            "source_id": row["source_id"],
            "status": status,
            "selected_url": selected.url if selected else None,
            "selected_kind": selected.kind if selected else None,
            "score": int(selected.score if selected else 0),
            "candidate_count": len(candidates),
            "errors": errors[:10],
            "next_retry_at": next_retry,
        }

    def process(self, row: dict[str, Any]) -> dict[str, Any]:
        self._demote_weak_registry_target(row)
        candidates: dict[str, Candidate] = {}
        errors: list[str] = []
        repository = normalize_repository_url(
            row.get("authority_repository") or row.get("canonical_repository")
        )
        website = canonical_documentation_url(
            row.get("authority_official_website") or row.get("official_website")
        )
        self._repository_candidates(repository, candidates, errors)
        self._website_candidates(website, candidates, errors)
        ordered = sorted(
            candidates.values(),
            key=lambda item: (-item.score, item.url),
        )
        selected = next((item for item in ordered if item.score >= DISCOVERY_ACCEPT_SCORE), None)
        if selected is not None:
            try:
                selected = self._validate_selected(selected)
            except Exception as exc:
                errors.append(f"selected target validation: {str(exc)[:500]}")
                selected = None
        if selected is not None:
            self._promote_target(row, selected)
            status = "DISCOVERED"
        elif errors:
            status = "RETRY"
        else:
            status = "NO_MATCH"
        return self._save_discovery(
            row,
            status=status,
            candidates=ordered,
            selected=selected,
            errors=errors,
        )

    def run(self, *, limit: int = 10, registry: str | None = None) -> dict[str, Any]:
        before_requests = self.network_requests
        rows = self._eligible(limit=limit, registry=registry)
        outcomes: list[dict[str, Any]] = []
        by_status: dict[str, int] = {}
        for row in rows:
            outcome = self.process(row)
            status = str(outcome["status"])
            by_status[status] = by_status.get(status, 0) + 1
            outcomes.append(outcome)
        return {
            "engine": "active-documentation-discovery-v1",
            "selected": len(rows),
            "processed": len(outcomes),
            "network_requests": self.network_requests - before_requests,
            "by_status": dict(sorted(by_status.items())),
            "discovered": by_status.get("DISCOVERED", 0),
            "retry": by_status.get("RETRY", 0),
            "no_match": by_status.get("NO_MATCH", 0),
            "review_exhausted": by_status.get("REVIEW_EXHAUSTED", 0),
            "outcomes": outcomes[:100],
        }

    def audit(self, *, top: int = 50) -> dict[str, Any]:
        status_rows = self.db.execute(
            "SELECT discovery_status,COUNT(*) AS n FROM documentation_discovery_results GROUP BY discovery_status"
        ).fetchall()
        weak_rows = self.db.execute(
            """
            SELECT COUNT(*) AS n FROM documentation_targets
            WHERE target_status='READY_FOR_DOCS_CONNECTOR'
              AND (target_url LIKE 'https://central.sonatype.com/%' OR target_url LIKE 'https://search.maven.org/%')
            """
        ).fetchone()
        pending_rows = self.db.execute(
            "SELECT COUNT(*) AS n FROM documentation_targets WHERE target_status='DOCS_DISCOVERY_REQUIRED'"
        ).fetchone()
        recent = [
            dict(row)
            for row in self.db.execute(
                """
                SELECT registry,name,source_id,discovery_status,selected_url,selected_kind,
                       selected_score,attempts,last_checked_at,next_retry_at,errors_json
                FROM documentation_discovery_results
                ORDER BY last_checked_at DESC,registry ASC,name ASC LIMIT ?
                """,
                (max(1, int(top)),),
            ).fetchall()
        ]
        return {
            "engine": "active-documentation-discovery-v1",
            "schema_version": DOCUMENTATION_DISCOVERY_SCHEMA_VERSION,
            "records": sum(int(row["n"]) for row in status_rows),
            "by_status": {str(row["discovery_status"]): int(row["n"]) for row in status_rows},
            "pending_discovery_targets": int(pending_rows["n"] if pending_rows else 0),
            "weak_registry_targets_still_ready": int(weak_rows["n"] if weak_rows else 0),
            "recent": recent,
        }
