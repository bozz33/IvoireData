from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from .http_client import HttpBudgetExceeded, http_run_context
from .settings import Settings
from .technology_authority_v2 import OfficialAuthorityResolver
from .technology_discovery import normalize_registry
from .technology_documentation import DocumentationTargetResolver
from .technology_documentation_discovery_runtime import ActiveDocumentationDiscovery
from .technology_documentation_fetch_v2 import DynamicDocumentationFetcher
from .technology_harvester import TechnologyHarvestQueue
from .technology_qualification import NATIVE_REGISTRY_ORDER
from .technology_qualification_v2 import TechnologyQualificationEngine


ORCHESTRATOR_SCHEMA_VERSION = 1
DEFAULT_LEASE_SECONDS = 2 * 60 * 60
DEFAULT_INTERVAL_SECONDS = 15 * 60
DEFAULT_GITHUB_COOLDOWN_SECONDS = 60 * 60


@dataclass(frozen=True)
class StageBudgets:
    qualification: int = 90
    authority: int = 36
    targets: int = 180
    discovery: int = 18
    fetch: int = 9


@dataclass(frozen=True)
class StageQuanta:
    qualification: int = 10
    authority: int = 4
    targets: int = 20
    discovery: int = 2
    fetch: int = 1


def _now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime | None = None) -> str:
    return (value or _now_dt()).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return max(minimum, int(default))
    try:
        return max(minimum, int(raw))
    except ValueError:
        return max(minimum, int(default))


def budgets_from_env() -> StageBudgets:
    return StageBudgets(
        qualification=_env_int("IVOIREDATA_TECH_QUALIFICATION_BUDGET", 90),
        authority=_env_int("IVOIREDATA_TECH_AUTHORITY_BUDGET", 36),
        targets=_env_int("IVOIREDATA_TECH_TARGET_BUDGET", 180),
        discovery=_env_int("IVOIREDATA_TECH_DISCOVERY_BUDGET", 18),
        fetch=_env_int("IVOIREDATA_TECH_FETCH_BUDGET", 9),
    )


def quanta_from_env() -> StageQuanta:
    return StageQuanta(
        qualification=_env_int("IVOIREDATA_TECH_QUALIFICATION_QUANTUM", 10, minimum=1),
        authority=_env_int("IVOIREDATA_TECH_AUTHORITY_QUANTUM", 4, minimum=1),
        targets=_env_int("IVOIREDATA_TECH_TARGET_QUANTUM", 20, minimum=1),
        discovery=_env_int("IVOIREDATA_TECH_DISCOVERY_QUANTUM", 2, minimum=1),
        fetch=_env_int("IVOIREDATA_TECH_FETCH_QUANTUM", 1, minimum=1),
    )


def _selected_count(payload: Any, requested: int) -> int:
    if not isinstance(payload, dict):
        return 0
    for key in ("selected", "processed", "resolved", "created"):
        try:
            if key in payload:
                return max(0, min(int(requested), int(payload.get(key) or 0)))
        except (TypeError, ValueError):
            pass
    outcomes = payload.get("outcomes")
    if isinstance(outcomes, list):
        return max(0, min(int(requested), len(outcomes)))
    return 0


def _github_rate_limit(payload: Any) -> tuple[bool, int | None, str | None]:
    if not isinstance(payload, dict):
        return False, None, None
    outcomes = payload.get("outcomes")
    if not isinstance(outcomes, list):
        return False, None, None
    best_delay: int | None = None
    reset: str | None = None
    hit = False
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        stats = outcome.get("stats")
        if not isinstance(stats, dict) or not stats.get("github_rate_limited"):
            continue
        hit = True
        try:
            delay = int(stats.get("github_retry_after_seconds") or 0) or None
        except (TypeError, ValueError):
            delay = None
        if delay is not None:
            best_delay = max(best_delay or 0, delay)
        if stats.get("github_rate_limit_reset"):
            reset = str(stats["github_rate_limit_reset"])
    return hit, best_delay, reset


class IndustrialTechnologyOrchestrator:
    """Bounded, crash-safe drain of the technology documentation pipeline.

    The bulk registry harvesters remain separate.  This orchestrator never bootstraps or
    re-downloads the registry universes; it only advances already-harvested SQLite rows
    through qualification -> authority -> target resolution -> active docs discovery ->
    official docs fetch.

    Fairness is implemented above every stage because later stages historically sort by
    quality/name and can otherwise be monopolized by one very large ecosystem.  Each
    stage has its own persistent registry rotation and quantum.  The underlying engines
    retain their own idempotency, ETag/SHA/version checks and retry state.
    """

    def __init__(
        self,
        *,
        queue: TechnologyHarvestQueue,
        settings: Settings,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self.queue = queue
        self.db = queue.db
        self.settings = settings
        self.lease_seconds = max(300, int(lease_seconds))
        self.qualifier = TechnologyQualificationEngine(queue=queue, user_agent=settings.user_agent)
        self.authority = OfficialAuthorityResolver(queue=queue, user_agent=settings.user_agent)
        self.targets = DocumentationTargetResolver(queue=queue)
        self.discovery = ActiveDocumentationDiscovery(queue=queue, user_agent=settings.user_agent)
        self.fetcher = DynamicDocumentationFetcher(queue=queue, settings=settings)
        self._init_schema()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS technology_orchestrator_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS technology_orchestrator_runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                run_status TEXT NOT NULL,
                budgets_json TEXT NOT NULL,
                quanta_json TEXT NOT NULL,
                registries_json TEXT NOT NULL,
                summary_json TEXT,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tech_orchestrator_runs_started
                ON technology_orchestrator_runs(started_at DESC);
            """
        )
        self._set_meta("schema_version", str(ORCHESTRATOR_SCHEMA_VERSION))
        self.db.commit()

    def _meta(self, key: str) -> str | None:
        row = self.db.execute(
            "SELECT value FROM technology_orchestrator_meta WHERE key=?",
            (key,),
        ).fetchone()
        return str(row["value"]) if row is not None else None

    def _set_meta_no_commit(self, key: str, value: str) -> None:
        self.db.execute(
            """
            INSERT INTO technology_orchestrator_meta(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (key, str(value), _iso()),
        )

    def _set_meta(self, key: str, value: str) -> None:
        with self.db:
            self._set_meta_no_commit(key, value)

    def _delete_meta_no_commit(self, key: str) -> None:
        self.db.execute("DELETE FROM technology_orchestrator_meta WHERE key=?", (key,))

    def acquire_lease(self, run_id: str) -> tuple[bool, dict[str, Any] | None]:
        now = _now_dt()
        with self.db:
            owner = self._meta("lease_owner")
            expiry_raw = self._meta("lease_expires_at")
            expiry = _parse_iso(expiry_raw)
            if owner and expiry and expiry > now and owner != run_id:
                return False, {"owner": owner, "expires_at": expiry_raw}
            self._set_meta_no_commit("lease_owner", run_id)
            self._set_meta_no_commit(
                "lease_expires_at",
                _iso(now + timedelta(seconds=self.lease_seconds)),
            )
        return True, None

    def heartbeat(self, run_id: str) -> None:
        with self.db:
            if self._meta("lease_owner") != run_id:
                raise RuntimeError("technology orchestrator lease was lost")
            self._set_meta_no_commit(
                "lease_expires_at",
                _iso(_now_dt() + timedelta(seconds=self.lease_seconds)),
            )

    def release_lease(self, run_id: str) -> None:
        with self.db:
            if self._meta("lease_owner") == run_id:
                self._delete_meta_no_commit("lease_owner")
                self._delete_meta_no_commit("lease_expires_at")

    def registries(self, requested: list[str] | None = None) -> list[str]:
        if requested:
            values = [normalize_registry(value) for value in requested if str(value).strip()]
            return list(dict.fromkeys(values))
        rows = self.db.execute(
            "SELECT registry,COUNT(*) AS n FROM candidates GROUP BY registry HAVING COUNT(*)>0"
        ).fetchall()
        available = {normalize_registry(str(row["registry"])) for row in rows}
        preferred = [registry for registry in NATIVE_REGISTRY_ORDER if registry in available]
        extras = sorted(available - set(preferred))
        return preferred + extras

    def _rotated(self, stage: str, registries: list[str]) -> list[str]:
        if len(registries) <= 1:
            return list(registries)
        last = self._meta(f"rotation:{stage}")
        if last not in registries:
            return list(registries)
        index = (registries.index(str(last)) + 1) % len(registries)
        return registries[index:] + registries[:index]

    def _record_rotation(self, stage: str, registry: str) -> None:
        self._set_meta(f"rotation:{stage}", registry)

    def _github_cooldown(self) -> dict[str, Any] | None:
        until_raw = self._meta("github_fetch_cooldown_until")
        until = _parse_iso(until_raw)
        if until is None:
            return None
        if until <= _now_dt():
            with self.db:
                self._delete_meta_no_commit("github_fetch_cooldown_until")
                self._delete_meta_no_commit("github_fetch_cooldown_reason")
            return None
        return {
            "until": until_raw,
            "reason": self._meta("github_fetch_cooldown_reason") or "GITHUB_RATE_LIMIT",
        }

    def _set_github_cooldown(self, *, delay_seconds: int | None, reset: str | None) -> dict[str, Any]:
        delay = max(300, int(delay_seconds or DEFAULT_GITHUB_COOLDOWN_SECONDS))
        reset_dt = _parse_iso(reset)
        until = max(
            _now_dt() + timedelta(seconds=delay),
            reset_dt or _now_dt(),
        )
        with self.db:
            self._set_meta_no_commit("github_fetch_cooldown_until", _iso(until))
            self._set_meta_no_commit(
                "github_fetch_cooldown_reason",
                "GITHUB_RATE_LIMIT_SHARED_BACKOFF",
            )
        return {"until": _iso(until), "delay_seconds": delay, "reset": reset}

    def _run_fair_stage(
        self,
        *,
        stage: str,
        registries: list[str],
        budget: int,
        quantum: int,
        runner: Callable[[str, int], dict[str, Any]],
        stop_on_github_rate_limit: bool = False,
        max_registry_errors: int = 3,
    ) -> dict[str, Any]:
        budget = max(0, int(budget))
        quantum = max(1, int(quantum))
        if budget == 0 or not registries:
            return {
                "stage": stage,
                "budget": budget,
                "quantum": quantum,
                "processed": 0,
                "calls": 0,
                "by_registry": {},
                "stopped_reason": "BUDGET_ZERO" if budget == 0 else "NO_REGISTRIES",
            }

        order = self._rotated(stage, registries)
        remaining = budget
        exhausted: set[str] = set()
        by_registry: dict[str, Any] = {}
        calls = 0
        processed = 0
        error_count = 0
        stopped_reason: str | None = None
        last_visited: str | None = None

        while remaining > 0 and len(exhausted) < len(order):
            made_progress = False
            for registry in order:
                if remaining <= 0:
                    break
                if registry in exhausted:
                    continue
                request_limit = min(quantum, remaining)
                last_visited = registry
                calls += 1
                try:
                    payload = runner(registry, request_limit)
                    count = _selected_count(payload, request_limit)
                    entry = by_registry.setdefault(
                        registry,
                        {"calls": 0, "processed": 0, "payloads": [], "errors": []},
                    )
                    entry["calls"] += 1
                    entry["processed"] += count
                    if len(entry["payloads"]) < 5:
                        entry["payloads"].append(payload)
                    processed += count
                    remaining -= count
                    if count < request_limit:
                        exhausted.add(registry)
                    if count > 0:
                        made_progress = True

                    if stop_on_github_rate_limit:
                        hit, delay, reset = _github_rate_limit(payload)
                        if hit:
                            cooldown = self._set_github_cooldown(
                                delay_seconds=delay,
                                reset=reset,
                            )
                            stopped_reason = "GITHUB_RATE_LIMIT"
                            entry["github_cooldown"] = cooldown
                            remaining = 0
                            break
                except Exception as exc:
                    entry = by_registry.setdefault(
                        registry,
                        {"calls": 0, "processed": 0, "payloads": [], "errors": []},
                    )
                    entry["calls"] += 1
                    entry["errors"].append(str(exc)[:1000])
                    exhausted.add(registry)
                    error_count += 1
                    if error_count >= max(1, int(max_registry_errors)):
                        stopped_reason = "REGISTRY_ERROR_LIMIT"
                        remaining = 0
                        break
            if not made_progress and stopped_reason is None:
                stopped_reason = "NO_ELIGIBLE_WORK"
                break

        if last_visited:
            self._record_rotation(stage, last_visited)
        return {
            "stage": stage,
            "budget": budget,
            "quantum": quantum,
            "processed": processed,
            "remaining_budget": max(0, remaining),
            "calls": calls,
            "registries_started_from": order[0] if order else None,
            "exhausted_registries": sorted(exhausted),
            "by_registry": by_registry,
            "stopped_reason": stopped_reason,
        }

    def run(
        self,
        *,
        budgets: StageBudgets | None = None,
        quanta: StageQuanta | None = None,
        registries: list[str] | None = None,
    ) -> dict[str, Any]:
        budgets = budgets or budgets_from_env()
        quanta = quanta or quanta_from_env()
        active = self.registries(registries)
        run_id = f"tech-orchestrator-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        acquired, holder = self.acquire_lease(run_id)
        if not acquired:
            return {
                "engine": "industrial-technology-orchestrator-v1",
                "run_id": run_id,
                "status": "SKIPPED_LEASE_HELD",
                "lease": holder,
                "registries": active,
            }

        started = _iso()
        with self.db:
            self.db.execute(
                """
                INSERT INTO technology_orchestrator_runs(
                    run_id,started_at,completed_at,run_status,budgets_json,quanta_json,
                    registries_json,summary_json,last_error
                ) VALUES(?, ?, NULL, 'RUNNING', ?, ?, ?, NULL, NULL)
                """,
                (
                    run_id,
                    started,
                    json.dumps(asdict(budgets), sort_keys=True),
                    json.dumps(asdict(quanta), sort_keys=True),
                    json.dumps(active, ensure_ascii=False),
                ),
            )

        summary: dict[str, Any] = {
            "engine": "industrial-technology-orchestrator-v1",
            "schema_version": ORCHESTRATOR_SCHEMA_VERSION,
            "run_id": run_id,
            "status": "RUNNING",
            "started_at": started,
            "registries": active,
            "budgets": asdict(budgets),
            "quanta": asdict(quanta),
            "stages": {},
        }
        try:
            summary["stages"]["qualification"] = self._run_fair_stage(
                stage="qualification",
                registries=active,
                budget=budgets.qualification,
                quantum=quanta.qualification,
                runner=lambda registry, limit: self.qualifier.run(limit=limit, registry=registry),
            )
            self.heartbeat(run_id)

            summary["stages"]["authority"] = self._run_fair_stage(
                stage="authority",
                registries=active,
                budget=budgets.authority,
                quantum=quanta.authority,
                runner=lambda registry, limit: self.authority.run(limit=limit, registry=registry),
            )
            self.heartbeat(run_id)

            summary["stages"]["targets"] = self._run_fair_stage(
                stage="targets",
                registries=active,
                budget=budgets.targets,
                quantum=quanta.targets,
                runner=lambda registry, limit: self.targets.run(limit=limit, registry=registry),
            )
            self.heartbeat(run_id)

            summary["stages"]["discovery"] = self._run_fair_stage(
                stage="discovery",
                registries=active,
                budget=budgets.discovery,
                quantum=quanta.discovery,
                runner=lambda registry, limit: self.discovery.run(limit=limit, registry=registry),
            )
            self.heartbeat(run_id)

            cooldown = self._github_cooldown()
            if cooldown:
                summary["stages"]["fetch"] = {
                    "stage": "fetch",
                    "budget": budgets.fetch,
                    "quantum": quanta.fetch,
                    "processed": 0,
                    "calls": 0,
                    "by_registry": {},
                    "stopped_reason": "GITHUB_SHARED_BACKOFF",
                    "github_cooldown": cooldown,
                }
            else:
                summary["stages"]["fetch"] = self._run_fair_stage(
                    stage="fetch",
                    registries=active,
                    budget=budgets.fetch,
                    quantum=quanta.fetch,
                    runner=lambda registry, limit: self.fetcher.run(
                        limit=limit,
                        registry=registry,
                        force=False,
                    ),
                    stop_on_github_rate_limit=True,
                )
            self.heartbeat(run_id)

            summary["status"] = "SUCCESS"
            summary["completed_at"] = _iso()
            summary["audit"] = self.audit(top=10, include_runs=False)
            with self.db:
                self.db.execute(
                    """
                    UPDATE technology_orchestrator_runs
                    SET completed_at=?,run_status='SUCCESS',summary_json=?,last_error=NULL
                    WHERE run_id=?
                    """,
                    (
                        summary["completed_at"],
                        json.dumps(summary, ensure_ascii=False, default=str),
                        run_id,
                    ),
                )
            return summary
        except Exception as exc:
            summary["status"] = "ERROR"
            summary["completed_at"] = _iso()
            summary["error"] = str(exc)[:2000]
            with self.db:
                self.db.execute(
                    """
                    UPDATE technology_orchestrator_runs
                    SET completed_at=?,run_status='ERROR',summary_json=?,last_error=?
                    WHERE run_id=?
                    """,
                    (
                        summary["completed_at"],
                        json.dumps(summary, ensure_ascii=False, default=str),
                        str(exc)[:2000],
                        run_id,
                    ),
                )
            raise
        finally:
            self.release_lease(run_id)

    def _group_count(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, int]:
        rows = self.db.execute(sql, params).fetchall()
        return {str(row["registry"]): int(row["n"] or 0) for row in rows}

    def audit(self, *, top: int = 20, include_runs: bool = True) -> dict[str, Any]:
        backlog = {
            "qualification_due": self._group_count(
                "SELECT registry,COUNT(*) AS n FROM candidates WHERE status IN ('PENDING','RETRY') GROUP BY registry"
            ),
            "ready_for_authority": self._group_count(
                "SELECT registry,COUNT(*) AS n FROM qualification_results WHERE qualification_status='READY_FOR_AUTHORITY' GROUP BY registry"
            ),
            "authority_verified": self._group_count(
                "SELECT registry,COUNT(*) AS n FROM authority_results WHERE authority_status='AUTHORITY_VERIFIED' GROUP BY registry"
            ),
            "docs_discovery_required": self._group_count(
                "SELECT registry,COUNT(*) AS n FROM documentation_targets WHERE target_status='DOCS_DISCOVERY_REQUIRED' GROUP BY registry"
            ),
            "docs_ready": self._group_count(
                "SELECT registry,COUNT(*) AS n FROM documentation_targets WHERE target_status='READY_FOR_DOCS_CONNECTOR' GROUP BY registry"
            ),
            "fetch_retry_or_partial": self._group_count(
                "SELECT registry,COUNT(*) AS n FROM documentation_fetch_state WHERE fetch_status IN ('RETRY','PARTIAL') GROUP BY registry"
            ),
        }
        rotations = {
            stage: self._meta(f"rotation:{stage}")
            for stage in ("qualification", "authority", "targets", "discovery", "fetch")
        }
        payload: dict[str, Any] = {
            "engine": "industrial-technology-orchestrator-v1",
            "schema_version": ORCHESTRATOR_SCHEMA_VERSION,
            "registries": self.registries(),
            "rotations": rotations,
            "github_fetch_cooldown": self._github_cooldown(),
            "backlog_by_registry": backlog,
            "lease": {
                "owner": self._meta("lease_owner"),
                "expires_at": self._meta("lease_expires_at"),
            },
        }
        if include_runs:
            rows = self.db.execute(
                """
                SELECT run_id,started_at,completed_at,run_status,budgets_json,quanta_json,
                       registries_json,last_error
                FROM technology_orchestrator_runs
                ORDER BY started_at DESC LIMIT ?
                """,
                (max(1, int(top)),),
            ).fetchall()
            payload["recent_runs"] = [dict(row) for row in rows]
        return payload


def _with_http_budget(
    *,
    settings: Settings,
    operation: Callable[[], dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"tech-orchestrator-http-{stamp}-{uuid4().hex[:8]}"
    context = http_run_context(
        source_id="technology:orchestrator",
        run_id=run_id,
        state_dir=settings.state_dir,
        user_agent=settings.user_agent,
        options={},
    )
    try:
        with context:
            payload = operation()
    except HttpBudgetExceeded as exc:
        return {
            "engine": "industrial-technology-orchestrator-v1",
            "status": "PARTIAL_HTTP_BUDGET",
            "error": str(exc),
            "http": context.snapshot(),
        }, 2
    payload = dict(payload)
    payload["http"] = context.snapshot()
    return payload, 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ivoiredata-tech-orchestrator",
        description="Bounded fair scheduler for qualification -> authority -> official documentation",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "loop"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--qualification-budget", type=int, default=None)
        cmd.add_argument("--authority-budget", type=int, default=None)
        cmd.add_argument("--target-budget", type=int, default=None)
        cmd.add_argument("--discovery-budget", type=int, default=None)
        cmd.add_argument("--fetch-budget", type=int, default=None)
        cmd.add_argument(
            "--registries",
            default=None,
            help="optional comma-separated registry aliases; default uses every harvested registry",
        )
        if name == "loop":
            cmd.add_argument(
                "--interval",
                type=int,
                default=None,
                help="seconds between bounded runs; minimum 300, default 900 or env override",
            )
    audit = sub.add_parser("audit")
    audit.add_argument("--top", type=int, default=20)
    return parser


def _budgets_from_args(args: argparse.Namespace) -> StageBudgets:
    base = budgets_from_env()
    return StageBudgets(
        qualification=base.qualification if args.qualification_budget is None else max(0, args.qualification_budget),
        authority=base.authority if args.authority_budget is None else max(0, args.authority_budget),
        targets=base.targets if args.target_budget is None else max(0, args.target_budget),
        discovery=base.discovery if args.discovery_budget is None else max(0, args.discovery_budget),
        fetch=base.fetch if args.fetch_budget is None else max(0, args.fetch_budget),
    )


def _registries_from_args(value: str | None) -> list[str] | None:
    if not value:
        return None
    values = [item.strip() for item in value.split(",") if item.strip()]
    return values or None


def _single_run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    settings = Settings.from_env()
    queue = TechnologyHarvestQueue(settings.state_dir / "technology_harvest.sqlite3")
    try:
        orchestrator = IndustrialTechnologyOrchestrator(queue=queue, settings=settings)
        budgets = _budgets_from_args(args)
        registries = _registries_from_args(args.registries)
        return _with_http_budget(
            settings=settings,
            operation=lambda: orchestrator.run(
                budgets=budgets,
                quanta=quanta_from_env(),
                registries=registries,
            ),
        )
    finally:
        queue.close()


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "audit":
        settings = Settings.from_env()
        queue = TechnologyHarvestQueue(settings.state_dir / "technology_harvest.sqlite3")
        try:
            payload = IndustrialTechnologyOrchestrator(queue=queue, settings=settings).audit(top=args.top)
        finally:
            queue.close()
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return 0

    if args.command == "run":
        payload, code = _single_run(args)
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return code

    interval = args.interval
    if interval is None:
        interval = _env_int(
            "IVOIREDATA_TECH_ORCHESTRATOR_INTERVAL",
            DEFAULT_INTERVAL_SECONDS,
            minimum=300,
        )
    interval = max(300, int(interval))
    while True:
        payload, _code = _single_run(args)
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str), flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
