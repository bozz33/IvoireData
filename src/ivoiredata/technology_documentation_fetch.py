from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from .delivery import ensure_source_layout, source_paths
from .models import SourceSpec, SyncResult
from .pipeline import get_source_pipeline
from .settings import Settings
from .state_io import load_json
from .technology_documentation import canonical_documentation_url
from .technology_harvester import TechnologyHarvestQueue


DOCUMENTATION_FETCH_SCHEMA_VERSION = 1
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_RETRY_BASE_SECONDS = 60 * 60
DEFAULT_RETRY_MAX_SECONDS = 24 * 60 * 60

Syncer = Callable[[SourceSpec, bool], SyncResult]


def _now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime | None = None) -> str:
    return (value or _now_dt()).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _language_domain(language: str) -> str:
    value = "".join(ch.casefold() if ch.isalnum() else "_" for ch in str(language or "general"))
    value = "_".join(part for part in value.split("_") if part)
    return f"programming_dynamic_{value or 'general'}"


class DynamicDocumentationFetcher:
    """Bounded stage-4 fetcher using the existing ``official_docs`` connector.

    The millions-scale package universe remains in SQLite. This class selects only a
    small bounded set of stage-3 documentation targets and constructs one temporary
    :class:`SourceSpec` at a time. It intentionally never injects dynamic packages into
    ``SourceRegistry`` and therefore cannot make application startup proportional to
    the global package count.

    The actual crawl/download path is unchanged: ``official_docs`` still owns sitemap
    discovery, canonical Git discovery, HTTP validators, SHA-256 comparison, local
    replay and chunk reuse. This layer only schedules and records dynamic targets.
    """

    def __init__(
        self,
        *,
        queue: TechnologyHarvestQueue,
        settings: Settings,
        syncer: Syncer | None = None,
        static_specs: Iterable[SourceSpec] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        retry_base_seconds: int = DEFAULT_RETRY_BASE_SECONDS,
        retry_max_seconds: int = DEFAULT_RETRY_MAX_SECONDS,
    ):
        self.queue = queue
        self.db = queue.db
        self.settings = settings
        self._syncer = syncer
        self._engine_instance = None
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_seconds = max(1, int(retry_base_seconds))
        self.retry_max_seconds = max(self.retry_base_seconds, int(retry_max_seconds))
        self._init_schema()
        if static_specs is None:
            static_specs = self._engine().registry.list(public_only=True)
        self.static_url_aliases = self._static_aliases(static_specs)

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS documentation_fetch_state (
                registry TEXT NOT NULL,
                name TEXT NOT NULL,
                source_id TEXT NOT NULL,
                target_url TEXT NOT NULL,
                target_resolved_at TEXT NOT NULL,
                fetch_status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                first_attempt_at TEXT,
                last_attempt_at TEXT,
                last_success_at TEXT,
                next_retry_at TEXT,
                alias_source_id TEXT,
                last_error TEXT,
                sync_details TEXT,
                stats_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (registry, name)
            );
            CREATE INDEX IF NOT EXISTS idx_docs_fetch_status_retry
                ON documentation_fetch_state(fetch_status,next_retry_at,attempts);
            CREATE INDEX IF NOT EXISTS idx_docs_fetch_target_success
                ON documentation_fetch_state(target_url,fetch_status);
            CREATE TABLE IF NOT EXISTS documentation_fetch_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.db.execute(
            """
            INSERT INTO documentation_fetch_meta(key,value,updated_at) VALUES('schema_version',?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (str(DOCUMENTATION_FETCH_SCHEMA_VERSION), _iso()),
        )
        self.db.commit()

    def _engine(self):
        if self._engine_instance is None:
            from .engine import IvoireDataEngine

            self._engine_instance = IvoireDataEngine(self.settings)
        return self._engine_instance

    @staticmethod
    def _canonical_target(target: dict[str, Any]) -> dict[str, Any]:
        canonical = canonical_documentation_url(target.get("target_url"))
        if not canonical:
            raise ValueError(f"invalid documentation target URL for {target.get('registry')}:{target.get('name')}")
        normalized = dict(target)
        normalized["target_url"] = canonical
        return normalized

    def _static_aliases(self, specs: Iterable[SourceSpec]) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for spec in specs:
            if spec.connector != "official_docs":
                continue
            canonical = canonical_documentation_url(spec.source_url)
            if canonical:
                aliases.setdefault(canonical, spec.source_id)
            public_docs = canonical_documentation_url(spec.options.get("public_docs_url"))
            if public_docs:
                aliases.setdefault(public_docs, spec.source_id)
        return aliases

    def _default_syncer(self, spec: SourceSpec, force: bool) -> SyncResult:
        """Run the standard connector/pipeline without adding the spec to the registry."""
        engine = self._engine()
        if not spec.enabled or not spec.public:
            raise PermissionError(f"dynamic documentation source is not publicly ingestible: {spec.source_id}")
        started = _iso()
        try:
            ensure_source_layout(self.settings, spec)
            pipeline = get_source_pipeline(self.settings, spec)
            details = str(
                pipeline.run(
                    engine._resource_for(spec, force=force),
                    loader_file_format="parquet",
                )
            )
            finished = _iso()
            engine.freshness.mark(spec.source_id, success=True, details=details)
            engine._write_manifest(
                spec,
                status="success",
                started=started,
                finished=finished,
                details=details,
            )
            return SyncResult(spec.source_id, "success", started, finished, spec.connector, details)
        except Exception as exc:
            finished = _iso()
            details = str(exc)
            engine.freshness.mark(spec.source_id, success=False, details=details)
            engine._write_manifest(
                spec,
                status="error",
                started=started,
                finished=finished,
                details=details,
            )
            return SyncResult(spec.source_id, "error", started, finished, spec.connector, details)

    def _run_syncer(self, spec: SourceSpec, force: bool) -> SyncResult:
        if self._syncer is not None:
            return self._syncer(spec, force)
        return self._default_syncer(spec, force)

    def _eligible_targets(self, *, limit: int, registry: str | None = None) -> list[dict[str, Any]]:
        if int(limit) <= 0:
            raise ValueError("dynamic documentation fetching is intentionally bounded; --limit must be > 0")
        where = [
            "d.target_status='READY_FOR_DOCS_CONNECTOR'",
            "d.target_url IS NOT NULL",
            "(f.registry IS NULL OR d.last_resolved_at>f.target_resolved_at "
            "OR (f.fetch_status IN ('RETRY','PARTIAL') AND f.attempts<? "
            "AND (f.next_retry_at IS NULL OR f.next_retry_at<=?)))",
        ]
        params: list[Any] = [self.max_attempts, _iso()]
        if registry:
            from .technology_discovery import normalize_registry

            where.append("d.registry=?")
            params.append(normalize_registry(registry))
        params.append(int(limit))
        rows = self.db.execute(
            """
            SELECT d.*
            FROM documentation_targets AS d
            LEFT JOIN documentation_fetch_state AS f
              ON f.registry=d.registry AND f.name=d.name
            WHERE """
            + " AND ".join(where)
            + " ORDER BY d.programming_language ASC,d.registry ASC,d.name ASC LIMIT ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def _spec(self, target: dict[str, Any]) -> SourceSpec:
        language = str(target.get("programming_language") or "General")
        canonical_name = str(target.get("canonical_name") or target["name"])
        target_url = str(target["target_url"])
        options = {
            "corpus_scope": "PROGRAMMING_DOCUMENTATION_DYNAMIC",
            "programming_language": language,
            "library": canonical_name,
            "ecosystem": target.get("ecosystem") or target.get("registry"),
            "doc_version": target.get("package_version"),
            "version_policy": "CURRENT_STABLE",
            "source_strategy": str(target.get("source_strategy") or "AUTO"),
            "public_docs_url": target_url,
            "canonical_repository": target.get("canonical_repository"),
            "package_purl": target.get("purl"),
            "package_registry": target.get("registry"),
            "package_name": target.get("name"),
            # Dynamic package documentation may be retained locally, but it is never
            # declared training-eligible until a separate rights/license review.
            "training_eligible": False,
            "license_review_status": "UNREVIEWED",
            "max_pages": 100_000,
            "max_sitemaps": 1_000,
            "max_bytes_per_page": 12_000_000,
            "max_new_bytes_per_run": 500_000_000,
            "request_pause_seconds": 0.02,
            "allow_crawl_fallback": True,
        }
        return SourceSpec(
            source_id=str(target["source_id"]),
            title=f"{canonical_name} Official Documentation",
            domain=_language_domain(language),
            provider=canonical_name,
            source_url=target_url,
            rights_tier="C_PUBLIC_LOCAL_INGEST",
            access_tier="OPEN_PUBLIC",
            priority="P3",
            connector="official_docs",
            refresh_hours=168,
            auto_sync=False,
            enabled=True,
            options=options,
        )

    def _stats(self, spec: SourceSpec) -> dict[str, Any]:
        path = source_paths(self.settings, spec)["raw"] / "official_docs_sync_stats.json"
        payload = load_json(path, {}) if path.exists() else {}
        return payload if isinstance(payload, dict) else {}

    def _completion(self, result: SyncResult, stats: dict[str, Any]) -> tuple[str, str | None]:
        if str(result.status).casefold() != "success":
            return "RETRY", str(result.details or "dynamic official_docs sync failed")[:1000]
        if not stats:
            return "PARTIAL", "official_docs sync stats missing"
        failed = int(stats.get("failed") or 0)
        backlog = int(stats.get("backlog_count") or 0)
        truncated = bool(stats.get("discovery_truncated"))
        complete = (
            bool(stats.get("discovery_complete"))
            and not truncated
            and failed == 0
            and backlog == 0
        )
        if complete:
            return "SUCCESS", None
        reason = (
            "incomplete official docs: "
            f"discovery_complete={bool(stats.get('discovery_complete'))} "
            f"truncated={truncated} failed={failed} backlog={backlog}"
        )
        return "PARTIAL", reason

    def _existing_dynamic_alias(self, target_url: str, registry: str, name: str) -> str | None:
        row = self.db.execute(
            """
            SELECT source_id FROM documentation_fetch_state
            WHERE target_url=?
              AND fetch_status IN ('SUCCESS','ALIASED_STATIC_SOURCE','ALIASED_DYNAMIC_SOURCE')
              AND NOT (registry=? AND name=?)
            ORDER BY last_success_at DESC,source_id ASC LIMIT 1
            """,
            (target_url, registry, name),
        ).fetchone()
        return str(row["source_id"]) if row else None

    def _save(
        self,
        target: dict[str, Any],
        *,
        status: str,
        alias_source_id: str | None = None,
        error: str | None = None,
        details: str | None = None,
        stats: dict[str, Any] | None = None,
        attempted: bool = False,
    ) -> dict[str, Any]:
        previous = self.db.execute(
            "SELECT attempts,first_attempt_at FROM documentation_fetch_state WHERE registry=? AND name=?",
            (target["registry"], target["name"]),
        ).fetchone()
        attempts = int(previous["attempts"] if previous else 0) + int(attempted)
        now = _iso()
        next_retry = None
        if status in {"RETRY", "PARTIAL"} and attempts < self.max_attempts:
            delay = min(
                self.retry_max_seconds,
                self.retry_base_seconds * (2 ** min(max(0, attempts - 1), 12)),
            )
            next_retry = _iso(_now_dt() + timedelta(seconds=delay))
        elif status in {"RETRY", "PARTIAL"} and attempts >= self.max_attempts:
            status = "REVIEW_EXHAUSTED"

        success_statuses = {"SUCCESS", "ALIASED_STATIC_SOURCE", "ALIASED_DYNAMIC_SOURCE"}
        last_success = now if status in success_statuses else None
        first_attempt_at = None
        if previous and previous["first_attempt_at"]:
            first_attempt_at = str(previous["first_attempt_at"])
        elif attempted:
            first_attempt_at = now

        with self.db:
            self.db.execute(
                """
                INSERT INTO documentation_fetch_state(
                    registry,name,source_id,target_url,target_resolved_at,fetch_status,attempts,
                    first_attempt_at,last_attempt_at,last_success_at,next_retry_at,alias_source_id,
                    last_error,sync_details,stats_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(registry,name) DO UPDATE SET
                    source_id=excluded.source_id,
                    target_url=excluded.target_url,
                    target_resolved_at=excluded.target_resolved_at,
                    fetch_status=excluded.fetch_status,
                    attempts=excluded.attempts,
                    first_attempt_at=COALESCE(documentation_fetch_state.first_attempt_at,excluded.first_attempt_at),
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=COALESCE(excluded.last_success_at,documentation_fetch_state.last_success_at),
                    next_retry_at=excluded.next_retry_at,
                    alias_source_id=excluded.alias_source_id,
                    last_error=excluded.last_error,
                    sync_details=excluded.sync_details,
                    stats_json=excluded.stats_json
                """,
                (
                    target["registry"],
                    target["name"],
                    target["source_id"],
                    target["target_url"],
                    target["last_resolved_at"],
                    status,
                    attempts,
                    first_attempt_at,
                    now if attempted else None,
                    last_success,
                    next_retry,
                    alias_source_id,
                    str(error)[:1000] if error else None,
                    str(details)[:4000] if details else None,
                    _json(stats or {}),
                ),
            )
        return {
            "registry": target["registry"],
            "name": target.get("canonical_name") or target["name"],
            "source_id": target["source_id"],
            "target_url": target["target_url"],
            "status": status,
            "alias_source_id": alias_source_id,
            "attempts": attempts,
            "next_retry_at": next_retry,
            "error": str(error)[:1000] if error else None,
            "stats": stats or {},
        }

    def process(self, target: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        # Stage 3 normally stores canonical URLs already. Canonicalizing again at the
        # fetch boundary makes de-duplication resilient to legacy rows/manual imports
        # and guarantees `.../docs/` cannot be downloaded again as `.../docs`.
        target = self._canonical_target(target)
        target_url = str(target["target_url"])

        static_alias = self.static_url_aliases.get(target_url)
        if static_alias:
            return self._save(
                target,
                status="ALIASED_STATIC_SOURCE",
                alias_source_id=static_alias,
                details="identical canonical documentation URL already covered by static official_docs source",
            )

        dynamic_alias = self._existing_dynamic_alias(
            target_url,
            str(target["registry"]),
            str(target["name"]),
        )
        if dynamic_alias:
            return self._save(
                target,
                status="ALIASED_DYNAMIC_SOURCE",
                alias_source_id=dynamic_alias,
                details="identical canonical documentation URL already fetched by another dynamic source",
            )

        spec = self._spec(target)
        result = self._run_syncer(spec, force)
        stats = self._stats(spec)
        status, error = self._completion(result, stats)
        return self._save(
            target,
            status=status,
            error=error,
            details=result.details,
            stats=stats,
            attempted=True,
        )

    def run(self, *, limit: int = 1, registry: str | None = None, force: bool = False) -> dict[str, Any]:
        targets = self._eligible_targets(limit=limit, registry=registry)
        outcomes: list[dict[str, Any]] = []
        by_status: dict[str, int] = {}
        for target in targets:
            try:
                outcome = self.process(target, force=force)
            except ValueError as exc:
                normalized = dict(target)
                normalized["target_url"] = str(target.get("target_url") or "")
                outcome = self._save(
                    normalized,
                    status="REVIEW_INVALID_TARGET",
                    error=str(exc),
                    details="target rejected before connector execution",
                    attempted=False,
                )
            status = str(outcome["status"])
            by_status[status] = by_status.get(status, 0) + 1
            outcomes.append(outcome)
        return {
            "engine": "dynamic-documentation-fetcher-v1",
            "selected": len(targets),
            "processed": len(outcomes),
            "by_status": dict(sorted(by_status.items())),
            "success": by_status.get("SUCCESS", 0),
            "aliased_static": by_status.get("ALIASED_STATIC_SOURCE", 0),
            "aliased_dynamic": by_status.get("ALIASED_DYNAMIC_SOURCE", 0),
            "partial": by_status.get("PARTIAL", 0),
            "retry": by_status.get("RETRY", 0),
            "review_exhausted": by_status.get("REVIEW_EXHAUSTED", 0),
            "invalid": by_status.get("REVIEW_INVALID_TARGET", 0),
            "outcomes": outcomes[:100],
        }

    def audit(self, *, top: int = 50) -> dict[str, Any]:
        status_rows = self.db.execute(
            "SELECT fetch_status,COUNT(*) AS n FROM documentation_fetch_state GROUP BY fetch_status"
        ).fetchall()
        total_ready = self.db.execute(
            "SELECT COUNT(*) AS n FROM documentation_targets WHERE target_status='READY_FOR_DOCS_CONNECTOR'"
        ).fetchone()
        covered = self.db.execute(
            """
            SELECT COUNT(*) AS n
            FROM documentation_targets AS d
            JOIN documentation_fetch_state AS f
              ON f.registry=d.registry AND f.name=d.name
            WHERE d.target_status='READY_FOR_DOCS_CONNECTOR'
              AND f.fetch_status IN ('SUCCESS','ALIASED_STATIC_SOURCE','ALIASED_DYNAMIC_SOURCE')
              AND f.target_resolved_at>=d.last_resolved_at
            """
        ).fetchone()
        recent = [
            dict(row)
            for row in self.db.execute(
                """
                SELECT registry,name,source_id,target_url,fetch_status,attempts,last_attempt_at,
                       last_success_at,next_retry_at,alias_source_id,last_error
                FROM documentation_fetch_state
                ORDER BY COALESCE(last_attempt_at,last_success_at,'') DESC,source_id ASC LIMIT ?
                """,
                (max(1, int(top)),),
            ).fetchall()
        ]
        ready_n = int(total_ready["n"] if total_ready else 0)
        covered_n = int(covered["n"] if covered else 0)
        return {
            "engine": "dynamic-documentation-fetcher-v1",
            "schema_version": DOCUMENTATION_FETCH_SCHEMA_VERSION,
            "ready_targets": ready_n,
            "covered_targets": covered_n,
            "remaining_targets": max(0, ready_n - covered_n),
            "coverage_percent": round((covered_n / ready_n) * 100, 2) if ready_n else 0.0,
            "by_status": {str(row["fetch_status"]): int(row["n"]) for row in status_rows},
            "recent": recent,
        }
