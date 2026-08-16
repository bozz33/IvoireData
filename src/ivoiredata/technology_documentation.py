from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .technology_discovery import normalize_registry, normalize_repository_url
from .technology_harvester import TechnologyHarvestQueue


DOCUMENTATION_TARGET_SCHEMA_VERSION = 1

# These labels are intentionally broad. They are used for corpus grouping and worker
# scheduling, not as a claim that every package in an ecosystem is written exclusively
# in that language.
_REGISTRY_LANGUAGE = {
    "npmjs.org": "JavaScript/TypeScript",
    "pypi.org": "Python",
    "packagist.org": "PHP",
    "crates.io": "Rust",
    "rubygems.org": "Ruby",
    "nuget.org": ".NET/C#",
    "repo1.maven.org": "JVM/Java",
    "proxy.golang.org": "Go",
    "pub.dev": "Dart/Flutter",
    "hex.pm": "Elixir/Erlang",
    "hackage.haskell.org": "Haskell",
    "cran.r-project.org": "R",
}

_TRACKING_QUERY_PREFIXES = (
    "utm_",
    "ref_",
)
_TRACKING_QUERY_KEYS = {
    "ref",
    "source",
    "campaign",
    "mc_cid",
    "mc_eid",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _valid_http_url(value: Any) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return False
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.hostname)


def canonical_documentation_url(value: Any) -> str | None:
    """Normalize a documentation URL without destroying functional query parameters."""
    if not _valid_http_url(value):
        return None
    parsed = urlparse(str(value).strip())
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold()
    port = parsed.port
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    kept_query = []
    for key, val in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered in _TRACKING_QUERY_KEYS or any(lowered.startswith(prefix) for prefix in _TRACKING_QUERY_PREFIXES):
            continue
        kept_query.append((key, val))
    query = urlencode(kept_query, doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


def _repo_parts(repository: str | None) -> tuple[str, str] | None:
    normalized = normalize_repository_url(repository)
    if not normalized:
        return None
    parsed = urlparse(normalized)
    if (parsed.hostname or "").casefold() != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    return parts[0].casefold(), parts[1].casefold()


def _github_docs_match(url: str | None, repository: str | None) -> bool:
    if not url:
        return False
    target = urlparse(url)
    if (target.hostname or "").casefold() != "github.com":
        return False
    parts = [part for part in target.path.split("/") if part]
    repo = _repo_parts(repository)
    return bool(repo and len(parts) >= 2 and (parts[0].casefold(), parts[1].casefold()) == repo)


def _host_relation(docs_url: str | None, website: str | None) -> bool:
    if not docs_url or not website or not _valid_http_url(website):
        return False
    docs_host = (urlparse(docs_url).hostname or "").casefold()
    website_host = (urlparse(str(website)).hostname or "").casefold()
    if not docs_host or not website_host:
        return False
    return docs_host == website_host or docs_host.endswith("." + website_host) or website_host.endswith("." + docs_host)


def _source_id(registry: str, name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:48] or "package"
    digest = hashlib.sha256(f"{registry}\0{name}".encode("utf-8")).hexdigest()[:12]
    ecosystem = re.sub(r"[^a-z0-9]+", "-", registry.casefold()).strip("-")[:20]
    return f"techdocs-{ecosystem}-{slug}-{digest}"


class DocumentationTargetResolver:
    """Bridge verified package authority into the existing official-docs ingestion path.

    This stage does not crawl or download documentation bodies. It materializes a
    compact, durable target only for packages whose stage-2 authority is VERIFIED.
    The existing ``official_docs`` connector remains the sole crawler/downloader and
    therefore keeps its sitemap/Git discovery, ETag/hash, 304 and content-reuse logic.

    A target is recalculated only after its authority decision changes (timestamp or
    authority attempt generation). Unchanged packages therefore cost zero network
    requests at this stage.
    """

    def __init__(self, *, queue: TechnologyHarvestQueue):
        self.queue = queue
        self.db = queue.db
        self._init_schema()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS documentation_targets (
                registry TEXT NOT NULL,
                name TEXT NOT NULL,
                source_id TEXT NOT NULL UNIQUE,
                ecosystem TEXT,
                programming_language TEXT,
                canonical_name TEXT,
                purl TEXT,
                package_version TEXT,
                canonical_repository TEXT,
                official_website TEXT,
                target_url TEXT,
                target_kind TEXT,
                source_strategy TEXT,
                target_status TEXT NOT NULL,
                target_confidence TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                authority_checked_at TEXT NOT NULL,
                authority_attempts INTEGER NOT NULL DEFAULT 0,
                first_resolved_at TEXT NOT NULL,
                last_resolved_at TEXT NOT NULL,
                PRIMARY KEY (registry, name)
            );
            CREATE INDEX IF NOT EXISTS idx_docs_target_status_language
                ON documentation_targets(target_status,programming_language,registry,name);
            CREATE INDEX IF NOT EXISTS idx_docs_target_url
                ON documentation_targets(target_url);
            CREATE TABLE IF NOT EXISTS documentation_target_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.db.execute(
            """
            INSERT INTO documentation_target_meta(key,value,updated_at) VALUES('schema_version',?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (str(DOCUMENTATION_TARGET_SCHEMA_VERSION), _now()),
        )
        self.db.commit()

    def _ready_authorities(self, *, limit: int, registry: str | None = None) -> list[dict[str, Any]]:
        if int(limit) <= 0:
            raise ValueError("documentation target resolution is intentionally bounded; --limit must be > 0")
        normalized = normalize_registry(registry) if registry else None
        where = [
            "a.authority_status='AUTHORITY_VERIFIED'",
            "(d.registry IS NULL OR a.last_checked_at>d.authority_checked_at OR a.attempts>d.authority_attempts)",
        ]
        params: list[Any] = []
        if normalized:
            where.append("a.registry=?")
            params.append(normalized)
        params.append(int(limit))
        rows = self.db.execute(
            """
            SELECT a.*,q.qualification_score,q.importance_score
            FROM authority_results AS a
            JOIN qualification_results AS q
              ON q.registry=a.registry AND q.name=a.name
            LEFT JOIN documentation_targets AS d
              ON d.registry=a.registry AND d.name=a.name
            WHERE """
            + " AND ".join(where)
            + " ORDER BY q.qualification_score DESC,q.importance_score DESC,a.name ASC LIMIT ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def _decision(self, authority: dict[str, Any]) -> dict[str, Any]:
        registry = normalize_registry(str(authority["registry"]))
        name = str(authority["name"])
        docs_url = canonical_documentation_url(authority.get("documentation_url"))
        website = canonical_documentation_url(authority.get("official_website"))
        repository = normalize_repository_url(authority.get("canonical_repository"))
        evidence = ["PACKAGE_AUTHORITY_VERIFIED"]

        if docs_url:
            target_url = docs_url
            target_kind = "DOCUMENTATION_URL"
            source_strategy = "AUTO"
            if _github_docs_match(docs_url, repository):
                confidence = "VERIFIED_REPOSITORY_RELATION"
                evidence.append("DOCUMENTATION_URL_MATCHES_CANONICAL_REPOSITORY")
            elif _host_relation(docs_url, website):
                confidence = "VERIFIED_WEBSITE_RELATION"
                evidence.append("DOCUMENTATION_HOST_MATCHES_OFFICIAL_WEBSITE")
            else:
                # This is still an authority-derived URL, but we deliberately do not
                # overclaim independent host verification. The official_docs connector
                # may later establish a canonical Git relationship from the page.
                confidence = "AUTHORITY_DERIVED"
                evidence.append("DOCUMENTATION_URL_FROM_VERIFIED_PACKAGE_METADATA")
            status = "READY_FOR_DOCS_CONNECTOR"
        elif website:
            target_url = website
            target_kind = "OFFICIAL_WEBSITE_DISCOVERY"
            source_strategy = "AUTO"
            confidence = "DISCOVERY_REQUIRED"
            status = "DOCS_DISCOVERY_REQUIRED"
            evidence.append("NO_DOCUMENTATION_URL_USE_OFFICIAL_WEBSITE_DISCOVERY")
        elif repository and _valid_http_url(repository):
            target_url = canonical_documentation_url(repository)
            target_kind = "CANONICAL_REPOSITORY_DISCOVERY"
            source_strategy = "AUTO"
            confidence = "DISCOVERY_REQUIRED"
            status = "DOCS_DISCOVERY_REQUIRED"
            evidence.append("NO_DOCUMENTATION_URL_USE_VERIFIED_REPOSITORY_DISCOVERY")
        else:
            target_url = None
            target_kind = "MISSING"
            source_strategy = "NONE"
            confidence = "MISSING"
            status = "DOCS_TARGET_MISSING"
            evidence.append("NO_DOCUMENTATION_WEBSITE_OR_REPOSITORY_TARGET")

        return {
            "registry": registry,
            "name": name,
            "source_id": _source_id(registry, name),
            "ecosystem": authority.get("ecosystem") or registry,
            "programming_language": _REGISTRY_LANGUAGE.get(registry, str(authority.get("ecosystem") or "General")),
            "canonical_name": authority.get("canonical_name") or name,
            "purl": authority.get("purl"),
            "package_version": authority.get("latest_stable_version"),
            "canonical_repository": repository,
            "official_website": website,
            "target_url": target_url,
            "target_kind": target_kind,
            "source_strategy": source_strategy,
            "target_status": status,
            "target_confidence": confidence,
            "evidence": evidence,
            "authority_checked_at": str(authority["last_checked_at"]),
            "authority_attempts": int(authority.get("attempts") or 0),
        }

    def _save(self, result: dict[str, Any]) -> None:
        previous = self.db.execute(
            "SELECT first_resolved_at FROM documentation_targets WHERE registry=? AND name=?",
            (result["registry"], result["name"]),
        ).fetchone()
        now = _now()
        with self.db:
            self.db.execute(
                """
                INSERT INTO documentation_targets(
                    registry,name,source_id,ecosystem,programming_language,canonical_name,purl,
                    package_version,canonical_repository,official_website,target_url,target_kind,
                    source_strategy,target_status,target_confidence,evidence_json,authority_checked_at,
                    authority_attempts,first_resolved_at,last_resolved_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(registry,name) DO UPDATE SET
                    source_id=excluded.source_id,
                    ecosystem=excluded.ecosystem,
                    programming_language=excluded.programming_language,
                    canonical_name=excluded.canonical_name,
                    purl=excluded.purl,
                    package_version=excluded.package_version,
                    canonical_repository=excluded.canonical_repository,
                    official_website=excluded.official_website,
                    target_url=excluded.target_url,
                    target_kind=excluded.target_kind,
                    source_strategy=excluded.source_strategy,
                    target_status=excluded.target_status,
                    target_confidence=excluded.target_confidence,
                    evidence_json=excluded.evidence_json,
                    authority_checked_at=excluded.authority_checked_at,
                    authority_attempts=excluded.authority_attempts,
                    last_resolved_at=excluded.last_resolved_at
                """,
                (
                    result["registry"],
                    result["name"],
                    result["source_id"],
                    result.get("ecosystem"),
                    result.get("programming_language"),
                    result.get("canonical_name"),
                    result.get("purl"),
                    result.get("package_version"),
                    result.get("canonical_repository"),
                    result.get("official_website"),
                    result.get("target_url"),
                    result.get("target_kind"),
                    result.get("source_strategy"),
                    result["target_status"],
                    result["target_confidence"],
                    _json(result.get("evidence") or []),
                    result["authority_checked_at"],
                    int(result.get("authority_attempts") or 0),
                    str(previous["first_resolved_at"]) if previous else now,
                    now,
                ),
            )

    def run(self, *, limit: int = 100, registry: str | None = None) -> dict[str, Any]:
        authorities = self._ready_authorities(limit=limit, registry=registry)
        outcomes: list[dict[str, Any]] = []
        by_status: dict[str, int] = {}
        by_language: dict[str, int] = {}
        for authority in authorities:
            result = self._decision(authority)
            self._save(result)
            status = str(result["target_status"])
            language = str(result["programming_language"])
            by_status[status] = by_status.get(status, 0) + 1
            by_language[language] = by_language.get(language, 0) + 1
            outcomes.append(
                {
                    "registry": result["registry"],
                    "name": result["canonical_name"],
                    "purl": result.get("purl"),
                    "version": result.get("package_version"),
                    "language": language,
                    "status": status,
                    "confidence": result["target_confidence"],
                    "target_url": result.get("target_url"),
                    "source_id": result["source_id"],
                }
            )
        return {
            "engine": "documentation-target-resolver-v1",
            "selected": len(authorities),
            "processed": len(outcomes),
            "by_status": dict(sorted(by_status.items())),
            "by_language": dict(sorted(by_language.items())),
            "ready_for_docs_connector": by_status.get("READY_FOR_DOCS_CONNECTOR", 0),
            "discovery_required": by_status.get("DOCS_DISCOVERY_REQUIRED", 0),
            "missing": by_status.get("DOCS_TARGET_MISSING", 0),
            "outcomes": outcomes[:100],
        }

    def audit(self, *, top: int = 50) -> dict[str, Any]:
        status_rows = self.db.execute(
            "SELECT target_status,COUNT(*) AS n FROM documentation_targets GROUP BY target_status"
        ).fetchall()
        language_rows = self.db.execute(
            """
            SELECT programming_language,COUNT(*) AS n,
                   SUM(CASE WHEN target_status='READY_FOR_DOCS_CONNECTOR' THEN 1 ELSE 0 END) AS ready
            FROM documentation_targets
            GROUP BY programming_language ORDER BY n DESC,programming_language ASC
            """
        ).fetchall()
        confidence_rows = self.db.execute(
            "SELECT target_confidence,COUNT(*) AS n FROM documentation_targets GROUP BY target_confidence"
        ).fetchall()
        ready_count = self.db.execute(
            "SELECT COUNT(*) AS n FROM documentation_targets WHERE target_status='READY_FOR_DOCS_CONNECTOR'"
        ).fetchone()
        ready = [
            dict(row)
            for row in self.db.execute(
                """
                SELECT source_id,registry,name,canonical_name,purl,package_version,
                       programming_language,target_url,target_confidence,canonical_repository,
                       authority_checked_at,last_resolved_at
                FROM documentation_targets
                WHERE target_status='READY_FOR_DOCS_CONNECTOR'
                ORDER BY programming_language ASC,name ASC LIMIT ?
                """,
                (max(1, int(top)),),
            ).fetchall()
        ]
        return {
            "engine": "documentation-target-resolver-v1",
            "schema_version": DOCUMENTATION_TARGET_SCHEMA_VERSION,
            "targets": sum(int(row["n"]) for row in status_rows),
            "ready_for_docs_connector": int(ready_count["n"] if ready_count else 0),
            "by_status": {str(row["target_status"]): int(row["n"]) for row in status_rows},
            "by_confidence": {str(row["target_confidence"]): int(row["n"]) for row in confidence_rows},
            "by_language": {
                str(row["programming_language"] or "General"): {
                    "targets": int(row["n"]),
                    "ready": int(row["ready"] or 0),
                }
                for row in language_rows
            },
            "top_ready": ready,
        }
