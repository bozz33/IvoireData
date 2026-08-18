from __future__ import annotations

from typing import Any

from . import technology_orchestrator as base
from .state_io import load_json


def _atomic_acquire_lease(
    self: base.IndustrialTechnologyOrchestrator,
    run_id: str,
) -> tuple[bool, dict[str, Any] | None]:
    """Acquire the process lease under a SQLite RESERVED lock.

    A normal sqlite3 context manager begins its transaction lazily on the first DML
    statement. Two processes could therefore both perform the pre-write SELECT before
    either owns a write lock. `BEGIN IMMEDIATE` serializes the read/check/write sequence
    itself, so only one worker can observe an expired/empty lease and claim it.
    """

    now = base._now_dt()
    self.db.execute("BEGIN IMMEDIATE")
    try:
        owner = self._meta("lease_owner")
        expiry_raw = self._meta("lease_expires_at")
        expiry = base._parse_iso(expiry_raw)
        if owner and expiry and expiry > now and owner != run_id:
            self.db.rollback()
            return False, {"owner": owner, "expires_at": expiry_raw}
        self._set_meta_no_commit("lease_owner", run_id)
        self._set_meta_no_commit(
            "lease_expires_at",
            base._iso(now + base.timedelta(seconds=self.lease_seconds)),
        )
        self.db.commit()
        return True, None
    except Exception:
        self.db.rollback()
        raise


_original_run_fair_stage = base.IndustrialTechnologyOrchestrator._run_fair_stage
_original_audit = base.IndustrialTechnologyOrchestrator.audit


def _clear_active_call(self: base.IndustrialTechnologyOrchestrator) -> None:
    with self.db:
        for key in (
            "active_stage",
            "active_registry",
            "active_call_started_at",
        ):
            self._delete_meta_no_commit(key)


def _observable_run_fair_stage(
    self: base.IndustrialTechnologyOrchestrator,
    *,
    stage: str,
    registries: list[str],
    budget: int,
    quantum: int,
    runner,
    stop_on_github_rate_limit: bool = False,
    max_registry_errors: int = 3,
):
    """Heartbeat the lease and expose the exact active stage around every quantum."""

    def watched_runner(registry: str, limit: int):
        owner = self._meta("lease_owner")
        if owner:
            self.heartbeat(owner)
        with self.db:
            self._set_meta_no_commit("active_stage", stage)
            self._set_meta_no_commit("active_registry", registry)
            self._set_meta_no_commit("active_call_started_at", base._iso())
        try:
            return runner(registry, limit)
        finally:
            current_owner = self._meta("lease_owner")
            if owner and current_owner == owner:
                self.heartbeat(owner)
            _clear_active_call(self)

    return _original_run_fair_stage(
        self,
        stage=stage,
        registries=registries,
        budget=budget,
        quantum=quantum,
        runner=watched_runner,
        stop_on_github_rate_limit=stop_on_github_rate_limit,
        max_registry_errors=max_registry_errors,
    )


def _audit_with_runtime_state(
    self: base.IndustrialTechnologyOrchestrator,
    *,
    top: int = 20,
    include_runs: bool = True,
) -> dict[str, Any]:
    payload = _original_audit(self, top=top, include_runs=include_runs)
    payload["active_call"] = {
        "stage": self._meta("active_stage"),
        "registry": self._meta("active_registry"),
        "started_at": self._meta("active_call_started_at"),
    }
    watchdog_path = self.settings.state_dir / "technology_fetch_active.json"
    watchdog = load_json(watchdog_path, {}) if watchdog_path.exists() else {}
    payload["fetch_watchdog"] = watchdog if isinstance(watchdog, dict) else {}
    return payload


# The public production entry point imports this module. Patch the class globals used by
# base._single_run/base.main before delegating, while retaining the implementation and
# tests in one orchestrator module.
base.IndustrialTechnologyOrchestrator.acquire_lease = _atomic_acquire_lease
base.IndustrialTechnologyOrchestrator._run_fair_stage = _observable_run_fair_stage
base.IndustrialTechnologyOrchestrator.audit = _audit_with_runtime_state
IndustrialTechnologyOrchestrator = base.IndustrialTechnologyOrchestrator
main = base.main
