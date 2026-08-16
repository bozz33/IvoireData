from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote

import requests

from .technology_discovery import (
    DEPS_DEV_API,
    ECOSYSTEMS_API,
    _DEPS_SYSTEMS,
    _deps_links,
    _extract_docs,
    _extract_repository,
    _first,
    normalize_repository_url,
    officiality_score,
    officiality_status,
)
from .technology_harvester import TechnologyHarvestQueue


AUTHORITY_SCHEMA_VERSION = 1
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_BASE_SECONDS = 30 * 60
DEFAULT_RETRY_MAX_SECONDS = 24 * 60 * 60

CrosscheckResolver = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def _now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime | None = None) -> str:
    return (value or _now_dt()).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: Any, fallback: Any) -> Any:
    try:
        loaded = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return loaded


def _merge_unique(*values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        items = value if isinstance(value, list) else [value]
        for item in items:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
    return out


class OfficialAuthorityResolver:
    """Stage-2 authority verification for qualified technology packages.

    Stage 1 already contacted the native package registry and persisted the complete
    native metadata in ``qualification_results.metadata_json``. This resolver reuses
    that exact evidence and only calls independent secondary sources. Consequently an
    authority pass never redownloads npm/PyPI/crates/NuGet/Maven/Go native metadata.

    A package is revisited only when its qualification timestamp is newer than the
    authority decision (a real upstream requeue/change) or when a persisted transient
    retry becomes due.
    """

    def __init__(
        self,
        *,
        queue: TechnologyHarvestQueue,
        user_agent: str,
        session: requests.Session | None = None,
        crosscheck_resolver: CrosscheckResolver | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_base_seconds: int = DEFAULT_RETRY_BASE_SECONDS,
        retry_max_seconds: int = DEFAULT_RETRY_MAX_SECONDS,
    ):
        self.queue = queue
        self.db = queue.db
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self.crosscheck_resolver = crosscheck_resolver
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_seconds = max(1, int(retry_base_seconds))
        self.retry_max_seconds = max(self.retry_base_seconds, int(retry_max_seconds))
        self._init_schema()

    def _init_schema(self) -> None:
        # qualification_results is intentionally not created here: authority resolution
        # has no source of truth without a completed stage-1 qualification.
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS authority_results (
                registry TEXT NOT NULL,
                name TEXT NOT NULL,
                ecosystem TEXT,
                canonical_name TEXT,
                purl TEXT,
                latest_stable_version TEXT,
                authority_status TEXT NOT NULL,
                officiality_score INTEGER NOT NULL DEFAULT 0,
                officiality_status TEXT,
                canonical_repository TEXT,
                alternate_repository TEXT,
                documentation_url TEXT,
                official_website TEXT,
                repository_match INTEGER NOT NULL DEFAULT 0,
                repository_conflict INTEGER NOT NULL DEFAULT 0,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                crosscheck_sources_json TEXT NOT NULL DEFAULT '[]',
                crosscheck_errors_json TEXT NOT NULL DEFAULT '[]',
                qualification_checked_at TEXT NOT NULL,
                first_checked_at TEXT NOT NULL,
                last_checked_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                last_error TEXT,
                PRIMARY KEY (registry, name)
            );
            CREATE INDEX IF NOT EXISTS idx_authority_status_score
                ON authority_results(authority_status, officiality_score DESC, last_checked_at DESC);
            CREATE INDEX IF NOT EXISTS idx_authority_ecosystem_score
                ON authority_results(ecosystem, officiality_score DESC);
            CREATE TABLE IF NOT EXISTS authority_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.db.execute(
            """
            INSERT INTO authority_meta(key,value,updated_at) VALUES('schema_version',?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (str(AUTHORITY_SCHEMA_VERSION), _iso()),
        )
        self.db.commit()

    def _get_json(self, url: str) -> tuple[dict[str, Any], str | None, bool]:
        """Return payload, transient-error message and whether the source responded.

        404 is a valid negative cross-check, not a transient failure. Other HTTP and
        transport errors are retryable evidence gaps.
        """
        try:
            response = self.session.get(
                url,
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                timeout=30,
            )
            if response.status_code == 404:
                return {}, None, True
            response.raise_for_status()
            payload = response.json()
            return (payload if isinstance(payload, dict) else {}), None, True
        except (requests.RequestException, ValueError) as exc:
            return {}, str(exc)[:500], False

    def _crosscheck(self, row: dict[str, Any], native: dict[str, Any]) -> dict[str, Any]:
        if self.crosscheck_resolver is not None:
            payload = self.crosscheck_resolver(row, native)
            return payload if isinstance(payload, dict) else {}

        registry = str(row["registry"])
        name = str(row.get("canonical_name") or row["name"])
        errors: list[str] = []
        sources: list[str] = []

        eco_url = f"{ECOSYSTEMS_API}/registries/{quote(registry, safe='')}/packages/{quote(name, safe='')}"
        eco, eco_error, eco_responded = self._get_json(eco_url)
        if eco_responded:
            sources.append("ecosyste.ms")
        if eco_error:
            errors.append(f"ecosyste.ms: {eco_error}")

        deps_package: dict[str, Any] = {}
        deps_version: dict[str, Any] = {}
        deps_system = _DEPS_SYSTEMS.get(registry)
        if deps_system:
            deps_url = f"{DEPS_DEV_API}/systems/{deps_system}/packages/{quote(name, safe='')}"
            deps_package, deps_error, deps_responded = self._get_json(deps_url)
            if deps_responded:
                sources.append("deps.dev")
            if deps_error:
                errors.append(f"deps.dev package: {deps_error}")
            versions = deps_package.get("versions") or []
            default = next(
                (item for item in versions if isinstance(item, dict) and item.get("isDefault")),
                None,
            )
            if default:
                version_key = default.get("versionKey") or {}
                version = str(version_key.get("version") or "").strip()
                if version:
                    version_url = (
                        f"{DEPS_DEV_API}/systems/{deps_system}/packages/{quote(name, safe='')}"
                        f"/versions/{quote(version, safe='')}"
                    )
                    deps_version, version_error, version_responded = self._get_json(version_url)
                    if version_responded and "deps.dev" not in sources:
                        sources.append("deps.dev")
                    if version_error:
                        errors.append(f"deps.dev version: {version_error}")

        links = _deps_links(deps_version)
        return {
            "ecosystems": eco,
            "deps_package": deps_package,
            "deps_version": deps_version,
            "deps_links": links,
            "sources": sources,
            "errors": errors,
        }

    def _ready_rows(self, *, limit: int, registry: str | None = None) -> list[dict[str, Any]]:
        if int(limit) <= 0:
            raise ValueError("authority resolution is intentionally bounded; --limit must be > 0")
        # If qualification_results does not exist SQLite raises a clear operational
        # error, which is preferable to inventing authority decisions without stage 1.
        where = [
            "q.qualification_status='READY_FOR_AUTHORITY'",
            "(a.registry IS NULL OR q.last_checked_at>a.qualification_checked_at "
            "OR (a.authority_status='AUTHORITY_PARTIAL_RETRY' AND a.attempts<? "
            "AND (a.next_retry_at IS NULL OR a.next_retry_at<=?)))",
        ]
        params: list[Any] = [self.max_attempts, _iso()]
        if registry:
            where.append("q.registry=?")
            params.append(registry)
        params.append(int(limit))
        rows = self.db.execute(
            """
            SELECT q.*
            FROM qualification_results AS q
            LEFT JOIN authority_results AS a
              ON a.registry=q.registry AND a.name=q.name
            WHERE """
            + " AND ".join(where)
            + " ORDER BY q.qualification_score DESC,q.importance_score DESC,q.last_checked_at ASC,q.name ASC LIMIT ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def _decision(
        self,
        row: dict[str, Any],
        native: dict[str, Any],
        cross: dict[str, Any],
    ) -> dict[str, Any]:
        eco = cross.get("ecosystems") if isinstance(cross.get("ecosystems"), dict) else {}
        links = cross.get("deps_links") if isinstance(cross.get("deps_links"), dict) else {}
        errors = [str(item) for item in (cross.get("errors") or []) if item]
        sources = _merge_unique(cross.get("sources") or [])

        native_repo = normalize_repository_url(native.get("canonical_repository"))
        eco_repo = normalize_repository_url(_extract_repository(eco))
        deps_repo = normalize_repository_url(
            links.get("SOURCE_REPO") or links.get("SOURCE") or links.get("REPOSITORY")
        )
        secondary_repos = [repo for repo in (eco_repo, deps_repo) if repo]
        repository_match = bool(native_repo and any(repo == native_repo for repo in secondary_repos))
        repository_conflict = bool(
            native_repo and any(repo != native_repo for repo in secondary_repos)
        ) or bool(eco_repo and deps_repo and eco_repo != deps_repo)

        canonical_repo = native_repo or eco_repo or deps_repo
        alternate_repo = next(
            (repo for repo in secondary_repos if canonical_repo and repo != canonical_repo),
            None,
        )
        corroborating_repo = None
        if repository_match:
            corroborating_repo = native_repo
        elif eco_repo and deps_repo and eco_repo == deps_repo:
            corroborating_repo = eco_repo

        homepage = (
            native.get("official_website")
            or _first(eco, "homepage", "homepage_url", "project_url")
            or links.get("HOMEPAGE")
        )
        docs = native.get("documentation_url") or _extract_docs(eco) or links.get("DOCUMENTATION")
        stable = str(row.get("latest_stable_version") or native.get("latest_stable_version") or "").strip() or None

        score, evidence = officiality_score(
            registry_repo=canonical_repo,
            deps_repo=corroborating_repo,
            homepage=str(homepage) if homepage else None,
            docs=str(docs) if docs else None,
            version=stable,
        )
        if native:
            score = min(100, score + 10)
            evidence = _merge_unique("NATIVE_REGISTRY_METADATA_REUSED", evidence)
        if native_repo and eco_repo and native_repo == eco_repo:
            evidence = _merge_unique(evidence, "ECOSYSTEMS_REPOSITORY_MATCH")
        if native_repo and deps_repo and native_repo == deps_repo:
            evidence = _merge_unique(evidence, "DEPSDEV_REPOSITORY_MATCH")
        if repository_conflict:
            score = min(score, 79)
            evidence = _merge_unique(evidence, "REPOSITORY_CONFLICT")

        # Automatic VERIFIED requires independent repository corroboration. Rich native
        # metadata alone remains PROBABLE until the documentation resolver verifies its
        # own host/repository relationship.
        if repository_conflict:
            status = "AUTHORITY_CONFLICT"
            next_action = "AUTHORITY_REVIEW"
        elif score >= 80 and repository_match:
            status = "AUTHORITY_VERIFIED"
            next_action = "DOCUMENTATION_RESOLUTION"
        elif errors:
            status = "AUTHORITY_PARTIAL_RETRY"
            next_action = "CROSSCHECK_RETRY"
        elif score >= 55:
            status = "AUTHORITY_PROBABLE"
            next_action = "AUTHORITY_REVIEW"
        else:
            status = "AUTHORITY_REVIEW"
            next_action = "AUTHORITY_REVIEW"

        return {
            "registry": str(row["registry"]),
            "name": str(row["name"]),
            "ecosystem": row.get("ecosystem"),
            "canonical_name": row.get("canonical_name") or native.get("name") or row["name"],
            "purl": row.get("purl"),
            "latest_stable_version": stable,
            "authority_status": status,
            "officiality_score": int(score),
            "officiality_status": officiality_status(score),
            "canonical_repository": canonical_repo,
            "alternate_repository": alternate_repo,
            "documentation_url": str(docs) if docs else None,
            "official_website": str(homepage) if homepage else None,
            "repository_match": repository_match,
            "repository_conflict": repository_conflict,
            "evidence": evidence,
            "crosscheck_sources": sources,
            "crosscheck_errors": errors,
            "qualification_checked_at": str(row["last_checked_at"]),
            "next_action": next_action,
        }

    def _save(self, result: dict[str, Any]) -> dict[str, Any]:
        previous = self.db.execute(
            "SELECT attempts,first_checked_at FROM authority_results WHERE registry=? AND name=?",
            (result["registry"], result["name"]),
        ).fetchone()
        attempts = int(previous["attempts"] if previous else 0) + 1
        now = _iso()
        status = str(result["authority_status"])
        errors = list(result.get("crosscheck_errors") or [])
        next_retry = None
        if status == "AUTHORITY_PARTIAL_RETRY":
            if attempts >= self.max_attempts:
                status = "AUTHORITY_REVIEW_EXHAUSTED"
                result["next_action"] = "AUTHORITY_REVIEW"
            else:
                delay = min(
                    self.retry_max_seconds,
                    self.retry_base_seconds * (2 ** min(attempts - 1, 12)),
                )
                next_retry = _iso(_now_dt() + timedelta(seconds=delay))
        last_error = "; ".join(errors)[:1000] if errors else None
        result["authority_status"] = status
        result["next_retry_at"] = next_retry
        result["attempts"] = attempts

        with self.db:
            self.db.execute(
                """
                INSERT INTO authority_results(
                    registry,name,ecosystem,canonical_name,purl,latest_stable_version,
                    authority_status,officiality_score,officiality_status,canonical_repository,
                    alternate_repository,documentation_url,official_website,repository_match,
                    repository_conflict,evidence_json,crosscheck_sources_json,crosscheck_errors_json,
                    qualification_checked_at,first_checked_at,last_checked_at,attempts,next_retry_at,last_error
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(registry,name) DO UPDATE SET
                    ecosystem=excluded.ecosystem,
                    canonical_name=excluded.canonical_name,
                    purl=excluded.purl,
                    latest_stable_version=excluded.latest_stable_version,
                    authority_status=excluded.authority_status,
                    officiality_score=excluded.officiality_score,
                    officiality_status=excluded.officiality_status,
                    canonical_repository=excluded.canonical_repository,
                    alternate_repository=excluded.alternate_repository,
                    documentation_url=excluded.documentation_url,
                    official_website=excluded.official_website,
                    repository_match=excluded.repository_match,
                    repository_conflict=excluded.repository_conflict,
                    evidence_json=excluded.evidence_json,
                    crosscheck_sources_json=excluded.crosscheck_sources_json,
                    crosscheck_errors_json=excluded.crosscheck_errors_json,
                    qualification_checked_at=excluded.qualification_checked_at,
                    last_checked_at=excluded.last_checked_at,
                    attempts=excluded.attempts,
                    next_retry_at=excluded.next_retry_at,
                    last_error=excluded.last_error
                """,
                (
                    result["registry"],
                    result["name"],
                    result.get("ecosystem"),
                    result.get("canonical_name"),
                    result.get("purl"),
                    result.get("latest_stable_version"),
                    status,
                    int(result.get("officiality_score") or 0),
                    result.get("officiality_status"),
                    result.get("canonical_repository"),
                    result.get("alternate_repository"),
                    result.get("documentation_url"),
                    result.get("official_website"),
                    int(bool(result.get("repository_match"))),
                    int(bool(result.get("repository_conflict"))),
                    _json(result.get("evidence") or []),
                    _json(result.get("crosscheck_sources") or []),
                    _json(errors),
                    result["qualification_checked_at"],
                    str(previous["first_checked_at"]) if previous else now,
                    now,
                    attempts,
                    next_retry,
                    last_error,
                ),
            )
        return result

    def process(self, row: dict[str, Any]) -> dict[str, Any]:
        native = _loads(row.get("metadata_json"), {})
        if not isinstance(native, dict):
            native = {}
        cross = self._crosscheck(row, native)
        return self._save(self._decision(row, native, cross))

    def run(self, *, limit: int = 25, registry: str | None = None) -> dict[str, Any]:
        rows = self._ready_rows(limit=limit, registry=registry)
        outcomes: list[dict[str, Any]] = []
        by_status: dict[str, int] = {}
        for row in rows:
            result = self.process(row)
            status = str(result["authority_status"])
            by_status[status] = by_status.get(status, 0) + 1
            outcomes.append(
                {
                    "registry": result["registry"],
                    "name": result.get("canonical_name") or result["name"],
                    "status": status,
                    "officiality_score": result.get("officiality_score"),
                    "repository": result.get("canonical_repository"),
                    "documentation_url": result.get("documentation_url"),
                    "repository_match": result.get("repository_match"),
                    "repository_conflict": result.get("repository_conflict"),
                    "crosscheck_sources": result.get("crosscheck_sources"),
                    "next_action": result.get("next_action"),
                    "next_retry_at": result.get("next_retry_at"),
                }
            )
        return {
            "engine": "official-authority-v1",
            "selected": len(rows),
            "processed": len(outcomes),
            "by_status": dict(sorted(by_status.items())),
            "verified": by_status.get("AUTHORITY_VERIFIED", 0),
            "probable": by_status.get("AUTHORITY_PROBABLE", 0),
            "conflicts": by_status.get("AUTHORITY_CONFLICT", 0),
            "retry": by_status.get("AUTHORITY_PARTIAL_RETRY", 0),
            "outcomes": outcomes[:100],
        }

    def audit(self, *, top: int = 20) -> dict[str, Any]:
        status_rows = self.db.execute(
            "SELECT authority_status,COUNT(*) AS n FROM authority_results GROUP BY authority_status"
        ).fetchall()
        ecosystem_rows = self.db.execute(
            "SELECT ecosystem,COUNT(*) AS n FROM authority_results GROUP BY ecosystem ORDER BY n DESC"
        ).fetchall()
        verified = [
            dict(row)
            for row in self.db.execute(
                """
                SELECT registry,name,ecosystem,canonical_name,purl,latest_stable_version,
                       officiality_score,canonical_repository,documentation_url,official_website,
                       crosscheck_sources_json,last_checked_at
                FROM authority_results
                WHERE authority_status='AUTHORITY_VERIFIED'
                ORDER BY officiality_score DESC,name ASC LIMIT ?
                """,
                (max(1, int(top)),),
            ).fetchall()
        ]
        return {
            "engine": "official-authority-v1",
            "schema_version": AUTHORITY_SCHEMA_VERSION,
            "authority_records": sum(int(row["n"]) for row in status_rows),
            "by_status": {str(row["authority_status"]): int(row["n"]) for row in status_rows},
            "by_ecosystem": {str(row["ecosystem"] or "UNKNOWN"): int(row["n"]) for row in ecosystem_rows},
            "verified_ready_for_docs": len(verified),
            "top_verified": verified,
        }
