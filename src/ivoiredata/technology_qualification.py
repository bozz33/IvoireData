from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

# Importing this module registers the Maven native adapter in technology_registries.
from . import technology_maven_authority as _technology_maven_authority  # noqa: F401
from .technology_discovery import (
    normalize_registry,
    normalize_repository_url,
    officiality_score,
    officiality_status,
)
from .technology_harvester import TechnologyHarvestQueue
from .technology_registries import build_purl, importance_score, native_package_metadata


QUALIFICATION_SCHEMA_VERSION = 1
FAST_PRIORITY = 70
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_BASE_SECONDS = 15 * 60
DEFAULT_RETRY_MAX_SECONDS = 24 * 60 * 60

# Native adapters that can currently qualify a package without relying on a
# third-party discovery index. The order is intentionally cross-ecosystem so a
# recently completed multi-million-package bootstrap cannot monopolize the
# qualification worker.
NATIVE_REGISTRY_ORDER = (
    "npmjs.org",
    "pypi.org",
    "packagist.org",
    "crates.io",
    "rubygems.org",
    "nuget.org",
    "repo1.maven.org",
    "proxy.golang.org",
)

_REGISTRY_ECOSYSTEM = {
    "npmjs.org": "javascript",
    "pypi.org": "python",
    "packagist.org": "php",
    "crates.io": "rust",
    "rubygems.org": "ruby",
    "nuget.org": "dotnet",
    "repo1.maven.org": "jvm",
    "proxy.golang.org": "go",
    "pub.dev": "dart",
    "hex.pm": "beam",
}

NativeResolver = Callable[[str, str], dict[str, Any] | None]


def _now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime | None = None) -> str:
    return (value or _now_dt()).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _bounded_int(value: Any, *, minimum: int = 0, maximum: int = 100) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    return max(minimum, min(maximum, number))


def _qualification_tier(score: int) -> str:
    if score >= 80:
        return "P0"
    if score >= 60:
        return "P1"
    if score >= 40:
        return "P2"
    return "P3"


def _http_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    try:
        return int(getattr(response, "status_code", 0) or 0) or None
    except (TypeError, ValueError):
        return None


class TechnologyQualificationEngine:
    """Scalable stage-1 qualification for the global technology universe.

    Discovery can contain millions of package names. This engine deliberately does
    *not* write every discovered package into technology_catalog.json and does not
    cross-check every package against secondary services. Instead it performs one
    native-authority resolution, stores the compact decision in SQLite, and promotes
    only promising candidates to the next authority/documentation stage.

    A candidate is resolved again only after a real upstream change requeues it, or
    after a transient failure reaches its retry time. This keeps qualification
    incremental and prevents repeated downloads/API work for unchanged packages.
    """

    def __init__(
        self,
        *,
        queue: TechnologyHarvestQueue,
        user_agent: str,
        session: requests.Session | None = None,
        native_resolver: NativeResolver | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_base_seconds: int = DEFAULT_RETRY_BASE_SECONDS,
        retry_max_seconds: int = DEFAULT_RETRY_MAX_SECONDS,
    ):
        self.queue = queue
        self.db = queue.db
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self.native_resolver = native_resolver
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_seconds = max(1, int(retry_base_seconds))
        self.retry_max_seconds = max(self.retry_base_seconds, int(retry_max_seconds))
        self._init_schema()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS qualification_results (
                registry TEXT NOT NULL,
                name TEXT NOT NULL,
                ecosystem TEXT,
                canonical_name TEXT,
                purl TEXT,
                latest_purl TEXT,
                latest_stable_version TEXT,
                authority_source TEXT,
                native_registry_url TEXT,
                canonical_repository TEXT,
                documentation_url TEXT,
                official_website TEXT,
                importance_score INTEGER NOT NULL DEFAULT 0,
                importance_tier TEXT,
                native_officiality_score INTEGER NOT NULL DEFAULT 0,
                native_officiality_status TEXT,
                qualification_score INTEGER NOT NULL DEFAULT 0,
                qualification_tier TEXT,
                qualification_status TEXT NOT NULL,
                candidate_priority INTEGER NOT NULL DEFAULT 0,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                first_checked_at TEXT NOT NULL,
                last_checked_at TEXT NOT NULL,
                next_retry_at TEXT,
                last_error TEXT,
                PRIMARY KEY (registry, name)
            );
            CREATE INDEX IF NOT EXISTS idx_qualification_status_score
                ON qualification_results(qualification_status, qualification_score DESC, last_checked_at DESC);
            CREATE INDEX IF NOT EXISTS idx_qualification_ecosystem_score
                ON qualification_results(ecosystem, qualification_score DESC);
            CREATE TABLE IF NOT EXISTS qualification_cursors (
                scope TEXT PRIMARY KEY,
                cursor TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS qualification_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.db.execute(
            """
            INSERT INTO qualification_meta(key,value,updated_at) VALUES('schema_version',?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (str(QUALIFICATION_SCHEMA_VERSION), _iso()),
        )
        self.db.commit()

    def _resolve_native(self, registry: str, name: str) -> dict[str, Any] | None:
        if self.native_resolver is not None:
            return self.native_resolver(registry, name)
        return native_package_metadata(
            registry,
            name,
            session=self.session,
            user_agent=self.user_agent,
        )

    def _cursor(self, scope: str) -> str:
        row = self.db.execute(
            "SELECT cursor FROM qualification_cursors WHERE scope=?",
            (scope,),
        ).fetchone()
        return str(row["cursor"] or "") if row else ""

    def _set_cursor_no_commit(self, scope: str, cursor: str) -> None:
        self.db.execute(
            """
            INSERT INTO qualification_cursors(scope,cursor,updated_at) VALUES(?,?,?)
            ON CONFLICT(scope) DO UPDATE SET cursor=excluded.cursor, updated_at=excluded.updated_at
            """,
            (scope, cursor, _iso()),
        )

    def _retry_allowed(self, candidate: dict[str, Any], *, now: str) -> bool:
        if int(candidate.get("attempts") or 0) >= self.max_attempts:
            return False
        if str(candidate.get("status") or "") != "RETRY":
            return True
        row = self.db.execute(
            "SELECT next_retry_at FROM qualification_results WHERE registry=? AND name=?",
            (candidate["registry"], candidate["name"]),
        ).fetchone()
        return not row or not row["next_retry_at"] or str(row["next_retry_at"]) <= now

    def _fast_candidates(
        self,
        *,
        limit: int,
        registry: str | None,
        min_priority: int,
        selected_keys: set[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        # The existing status/priority index makes this lane cheap even with a
        # multi-million-row candidate table. A bounded over-read lets us skip
        # candidates waiting for retry backoff without scanning the universe.
        pool = self.queue.pending(limit=max(100, min(5000, limit * 20)))
        now = _iso()
        out: list[dict[str, Any]] = []
        for candidate in pool:
            key = (str(candidate["registry"]), str(candidate["name"]))
            if key in selected_keys:
                continue
            if registry and key[0] != registry:
                continue
            priority = int(candidate.get("priority") or 0)
            if priority < max(FAST_PRIORITY, min_priority):
                continue
            if not self._retry_allowed(candidate, now=now):
                continue
            row = dict(candidate)
            row["_qualification_lane"] = "fast"
            out.append(row)
            selected_keys.add(key)
            if len(out) >= limit:
                break
        return out

    def _fair_candidate(
        self,
        registry: str,
        *,
        after_name: str,
        min_priority: int,
        selected_keys: set[tuple[str, str]],
        wrapped: bool,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        now = _iso()

        def fetch(after: str, *, before_or_equal: str | None = None) -> list[Any]:
            where = [
                "c.registry=?",
                "c.name>?",
                "c.status IN ('PENDING','RETRY')",
                "c.priority>=?",
                "c.attempts<?",
            ]
            params: list[Any] = [registry, after, int(min_priority), self.max_attempts]
            if before_or_equal is not None:
                where.append("c.name<=?")
                params.append(before_or_equal)
            return self.db.execute(
                "SELECT c.* FROM candidates AS c WHERE "
                + " AND ".join(where)
                + " ORDER BY c.name ASC LIMIT 64",
                params,
            ).fetchall()

        rows = fetch(after_name)
        did_wrap = wrapped
        if not rows and after_name and not wrapped:
            rows = fetch("", before_or_equal=after_name)
            did_wrap = True
        for raw in rows:
            candidate = dict(raw)
            key = (str(candidate["registry"]), str(candidate["name"]))
            if key in selected_keys:
                continue
            if not self._retry_allowed(candidate, now=now):
                continue
            candidate["_qualification_lane"] = "fair"
            return candidate, str(candidate["name"]), did_wrap
        return None, after_name, did_wrap

    def select(
        self,
        *,
        limit: int,
        registry: str | None = None,
        min_priority: int = 0,
        fast_only: bool = False,
    ) -> list[dict[str, Any]]:
        if int(limit) <= 0:
            raise ValueError(
                "qualification is intentionally bounded; --limit must be > 0 so millions of packages are not resolved in one run"
            )
        limit = int(limit)
        min_priority = max(0, int(min_priority))
        normalized_registry = normalize_registry(registry) if registry else None
        selected: list[dict[str, Any]] = []
        selected_keys: set[tuple[str, str]] = set()

        fast_target = limit if fast_only else max(1, min(limit, limit // 3 or 1))
        selected.extend(
            self._fast_candidates(
                limit=fast_target,
                registry=normalized_registry,
                min_priority=min_priority,
                selected_keys=selected_keys,
            )
        )
        if fast_only or len(selected) >= limit:
            return selected[:limit]

        registries = [normalized_registry] if normalized_registry else list(NATIVE_REGISTRY_ORDER)
        if not registries:
            return selected

        last_registry = self._cursor("registry-rotation")
        if last_registry in registries:
            index = (registries.index(last_registry) + 1) % len(registries)
            registries = registries[index:] + registries[:index]

        local_cursor = {reg: self._cursor(f"registry:{reg}") for reg in registries}
        wrapped = {reg: False for reg in registries}
        exhausted: set[str] = set()
        last_fair_registry = ""

        while len(selected) < limit and len(exhausted) < len(registries):
            made_progress = False
            for reg in registries:
                if len(selected) >= limit:
                    break
                if reg in exhausted:
                    continue
                candidate, cursor, did_wrap = self._fair_candidate(
                    reg,
                    after_name=local_cursor[reg],
                    min_priority=min_priority,
                    selected_keys=selected_keys,
                    wrapped=wrapped[reg],
                )
                local_cursor[reg] = cursor
                wrapped[reg] = did_wrap
                if candidate is None:
                    exhausted.add(reg)
                    continue
                key = (str(candidate["registry"]), str(candidate["name"]))
                selected.append(candidate)
                selected_keys.add(key)
                last_fair_registry = reg
                made_progress = True
            if not made_progress:
                break

        # The rotation cursor is only a scheduling hint. Per-registry package
        # cursors advance transactionally after each processed candidate.
        if last_fair_registry:
            with self.db:
                self._set_cursor_no_commit("registry-rotation", last_fair_registry)
        return selected

    def _decision(self, candidate: dict[str, Any], native: dict[str, Any]) -> dict[str, Any]:
        registry = normalize_registry(str(candidate["registry"]))
        original_name = str(candidate["name"])
        canonical_name = str(native.get("name") or original_name).strip() or original_name
        stable = str(native.get("latest_stable_version") or "").strip() or None
        repository = normalize_repository_url(native.get("canonical_repository"))
        docs = str(native.get("documentation_url") or "").strip() or None
        website = str(native.get("official_website") or "").strip() or None

        native_score, evidence = officiality_score(
            registry_repo=repository,
            deps_repo=None,
            homepage=website,
            docs=docs,
            version=stable,
        )
        native_score = min(100, native_score + 10)
        evidence = ["NATIVE_REGISTRY_METADATA", *evidence]

        importance_input = dict(native)
        importance_input["officiality_score"] = native_score
        importance, importance_tier = importance_score(importance_input)
        priority = _bounded_int(candidate.get("priority"))
        qualification = min(
            100,
            int(round(importance * 0.45 + native_score * 0.35 + priority * 0.20)),
        )
        tier = _qualification_tier(qualification)

        if stable and (
            (native_score >= 55 and qualification >= 40)
            or (priority >= 80 and native_score >= 30)
        ):
            status = "READY_FOR_AUTHORITY"
            action = "AUTHORITY_RESOLUTION"
        elif stable and native_score >= 30:
            status = "QUALIFIED_ON_DEMAND"
            action = "ON_DEMAND"
        else:
            status = "NEEDS_METADATA"
            action = "METADATA_REVIEW"

        return {
            "registry": registry,
            "name": original_name,
            "ecosystem": _REGISTRY_ECOSYSTEM.get(registry, registry),
            "canonical_name": canonical_name,
            "purl": build_purl(registry, canonical_name),
            "latest_purl": build_purl(registry, canonical_name, stable) if stable else None,
            "latest_stable_version": stable,
            "authority_source": native.get("authority_source") or registry,
            "native_registry_url": native.get("native_registry_url"),
            "canonical_repository": repository,
            "documentation_url": docs,
            "official_website": website,
            "importance_score": importance,
            "importance_tier": importance_tier,
            "native_officiality_score": native_score,
            "native_officiality_status": officiality_status(native_score),
            "qualification_score": qualification,
            "qualification_tier": tier,
            "qualification_status": status,
            "candidate_priority": priority,
            "evidence": evidence,
            "metadata": native,
            "next_action": action,
        }

    def _upsert_result_no_commit(
        self,
        result: dict[str, Any],
        *,
        next_retry_at: str | None = None,
        last_error: str | None = None,
    ) -> None:
        now = _iso()
        self.db.execute(
            """
            INSERT INTO qualification_results(
                registry,name,ecosystem,canonical_name,purl,latest_purl,latest_stable_version,
                authority_source,native_registry_url,canonical_repository,documentation_url,
                official_website,importance_score,importance_tier,native_officiality_score,
                native_officiality_status,qualification_score,qualification_tier,
                qualification_status,candidate_priority,evidence_json,metadata_json,
                first_checked_at,last_checked_at,next_retry_at,last_error
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(registry,name) DO UPDATE SET
                ecosystem=COALESCE(excluded.ecosystem,qualification_results.ecosystem),
                canonical_name=COALESCE(excluded.canonical_name,qualification_results.canonical_name),
                purl=COALESCE(excluded.purl,qualification_results.purl),
                latest_purl=COALESCE(excluded.latest_purl,qualification_results.latest_purl),
                latest_stable_version=COALESCE(excluded.latest_stable_version,qualification_results.latest_stable_version),
                authority_source=COALESCE(excluded.authority_source,qualification_results.authority_source),
                native_registry_url=COALESCE(excluded.native_registry_url,qualification_results.native_registry_url),
                canonical_repository=COALESCE(excluded.canonical_repository,qualification_results.canonical_repository),
                documentation_url=COALESCE(excluded.documentation_url,qualification_results.documentation_url),
                official_website=COALESCE(excluded.official_website,qualification_results.official_website),
                importance_score=excluded.importance_score,
                importance_tier=COALESCE(excluded.importance_tier,qualification_results.importance_tier),
                native_officiality_score=excluded.native_officiality_score,
                native_officiality_status=COALESCE(excluded.native_officiality_status,qualification_results.native_officiality_status),
                qualification_score=excluded.qualification_score,
                qualification_tier=COALESCE(excluded.qualification_tier,qualification_results.qualification_tier),
                qualification_status=excluded.qualification_status,
                candidate_priority=excluded.candidate_priority,
                evidence_json=excluded.evidence_json,
                metadata_json=excluded.metadata_json,
                last_checked_at=excluded.last_checked_at,
                next_retry_at=excluded.next_retry_at,
                last_error=excluded.last_error
            """,
            (
                result["registry"],
                result["name"],
                result.get("ecosystem"),
                result.get("canonical_name"),
                result.get("purl"),
                result.get("latest_purl"),
                result.get("latest_stable_version"),
                result.get("authority_source"),
                result.get("native_registry_url"),
                result.get("canonical_repository"),
                result.get("documentation_url"),
                result.get("official_website"),
                int(result.get("importance_score") or 0),
                result.get("importance_tier"),
                int(result.get("native_officiality_score") or 0),
                result.get("native_officiality_status"),
                int(result.get("qualification_score") or 0),
                result.get("qualification_tier"),
                result["qualification_status"],
                int(result.get("candidate_priority") or 0),
                _json(result.get("evidence") or []),
                _json(result.get("metadata") or {}),
                now,
                now,
                next_retry_at,
                str(last_error)[:1000] if last_error else None,
            ),
        )

    def _finish_valid(self, candidate: dict[str, Any], result: dict[str, Any]) -> None:
        with self.db:
            self._upsert_result_no_commit(result)
            self.db.execute(
                """
                UPDATE candidates
                SET status='QUALIFIED', attempts=attempts+1, last_error=NULL
                WHERE registry=? AND name=?
                """,
                (candidate["registry"], candidate["name"]),
            )
            if candidate.get("_qualification_lane") == "fair":
                self._set_cursor_no_commit(
                    f"registry:{candidate['registry']}",
                    str(candidate["name"]),
                )

    def _finish_terminal(self, candidate: dict[str, Any], *, status: str, error: str) -> dict[str, Any]:
        result = {
            "registry": normalize_registry(str(candidate["registry"])),
            "name": str(candidate["name"]),
            "ecosystem": _REGISTRY_ECOSYSTEM.get(normalize_registry(str(candidate["registry"])), str(candidate["registry"])),
            "qualification_status": status,
            "candidate_priority": _bounded_int(candidate.get("priority")),
            "evidence": [],
            "metadata": {},
        }
        with self.db:
            self._upsert_result_no_commit(result, last_error=error)
            self.db.execute(
                """
                UPDATE candidates
                SET status=?, attempts=attempts+1, last_error=?
                WHERE registry=? AND name=?
                """,
                (status, str(error)[:1000], candidate["registry"], candidate["name"]),
            )
            if candidate.get("_qualification_lane") == "fair":
                self._set_cursor_no_commit(f"registry:{candidate['registry']}", str(candidate["name"]))
        return result

    def _finish_retry(self, candidate: dict[str, Any], exc: Exception) -> dict[str, Any]:
        attempts_before = max(0, int(candidate.get("attempts") or 0))
        delay = min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2 ** min(attempts_before, 12)),
        )
        next_retry = _iso(_now_dt() + timedelta(seconds=delay))
        error = str(exc)[:1000]
        result = {
            "registry": normalize_registry(str(candidate["registry"])),
            "name": str(candidate["name"]),
            "ecosystem": _REGISTRY_ECOSYSTEM.get(normalize_registry(str(candidate["registry"])), str(candidate["registry"])),
            "qualification_status": "RETRY",
            "candidate_priority": _bounded_int(candidate.get("priority")),
            "evidence": [],
            "metadata": {},
        }
        with self.db:
            self._upsert_result_no_commit(result, next_retry_at=next_retry, last_error=error)
            self.db.execute(
                """
                UPDATE candidates
                SET status='RETRY', attempts=attempts+1, last_error=?
                WHERE registry=? AND name=?
                """,
                (error, candidate["registry"], candidate["name"]),
            )
            if candidate.get("_qualification_lane") == "fair":
                self._set_cursor_no_commit(f"registry:{candidate['registry']}", str(candidate["name"]))
        result["next_retry_at"] = next_retry
        result["last_error"] = error
        return result

    def process_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        registry = normalize_registry(str(candidate["registry"]))
        name = str(candidate["name"])
        try:
            native = self._resolve_native(registry, name)
            if native is None:
                return self._finish_terminal(
                    candidate,
                    status="DEFERRED_UNSUPPORTED",
                    error=f"no native qualification adapter for {registry}",
                )
            result = self._decision(candidate, native)
            self._finish_valid(candidate, result)
            return result
        except Exception as exc:
            if _http_status(exc) == 404:
                return self._finish_terminal(
                    candidate,
                    status="NOT_FOUND",
                    error=str(exc) or "native registry returned 404",
                )
            return self._finish_retry(candidate, exc)

    def run(
        self,
        *,
        limit: int = 50,
        registry: str | None = None,
        min_priority: int = 0,
        fast_only: bool = False,
    ) -> dict[str, Any]:
        selected = self.select(
            limit=limit,
            registry=registry,
            min_priority=min_priority,
            fast_only=fast_only,
        )
        outcomes: list[dict[str, Any]] = []
        by_status: dict[str, int] = {}
        by_registry: dict[str, int] = {}
        for candidate in selected:
            result = self.process_candidate(candidate)
            status = str(result.get("qualification_status") or "UNKNOWN")
            reg = str(result.get("registry") or candidate.get("registry") or "UNKNOWN")
            by_status[status] = by_status.get(status, 0) + 1
            by_registry[reg] = by_registry.get(reg, 0) + 1
            outcomes.append(
                {
                    "registry": reg,
                    "name": result.get("canonical_name") or result.get("name"),
                    "status": status,
                    "score": int(result.get("qualification_score") or 0),
                    "tier": result.get("qualification_tier"),
                    "next_action": result.get("next_action"),
                    "latest_stable_version": result.get("latest_stable_version"),
                    "purl": result.get("purl"),
                    "next_retry_at": result.get("next_retry_at"),
                    "error": result.get("last_error"),
                }
            )
        return {
            "engine": "qualification-v1",
            "selected": len(selected),
            "processed": len(outcomes),
            "by_status": dict(sorted(by_status.items())),
            "by_registry": dict(sorted(by_registry.items())),
            "ready_for_authority": by_status.get("READY_FOR_AUTHORITY", 0),
            "on_demand": by_status.get("QUALIFIED_ON_DEMAND", 0),
            "needs_metadata": by_status.get("NEEDS_METADATA", 0),
            "retry": by_status.get("RETRY", 0),
            "terminal": by_status.get("NOT_FOUND", 0) + by_status.get("DEFERRED_UNSUPPORTED", 0),
            "outcomes": outcomes[:100],
        }

    def audit(self, *, top: int = 20) -> dict[str, Any]:
        status_rows = self.db.execute(
            "SELECT qualification_status,COUNT(*) AS n FROM qualification_results GROUP BY qualification_status"
        ).fetchall()
        tier_rows = self.db.execute(
            "SELECT qualification_tier,COUNT(*) AS n FROM qualification_results GROUP BY qualification_tier"
        ).fetchall()
        ecosystem_rows = self.db.execute(
            "SELECT ecosystem,COUNT(*) AS n FROM qualification_results GROUP BY ecosystem ORDER BY n DESC"
        ).fetchall()
        total = sum(int(row["n"]) for row in status_rows)
        retry_exhausted = self.db.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE status='RETRY' AND attempts>=?",
            (self.max_attempts,),
        ).fetchone()
        ready = [
            dict(row)
            for row in self.db.execute(
                """
                SELECT registry,name,ecosystem,canonical_name,purl,latest_stable_version,
                       qualification_score,qualification_tier,native_officiality_score,
                       importance_score,documentation_url,canonical_repository,last_checked_at
                FROM qualification_results
                WHERE qualification_status='READY_FOR_AUTHORITY'
                ORDER BY qualification_score DESC,native_officiality_score DESC,name ASC
                LIMIT ?
                """,
                (max(1, int(top)),),
            ).fetchall()
        ]
        return {
            "engine": "qualification-v1",
            "schema_version": QUALIFICATION_SCHEMA_VERSION,
            "qualified_records": total,
            "by_status": {str(row["qualification_status"]): int(row["n"]) for row in status_rows},
            "by_tier": {str(row["qualification_tier"] or "UNSCORED"): int(row["n"]) for row in tier_rows},
            "by_ecosystem": {str(row["ecosystem"] or "UNKNOWN"): int(row["n"]) for row in ecosystem_rows},
            "retry_exhausted": int(retry_exhausted["n"] if retry_exhausted else 0),
            "top_ready_for_authority": ready,
        }
