from __future__ import annotations

from typing import Any

from . import technology_orchestrator as base


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


# The public production entry point imports this module. Patch the class global used by
# base._single_run/base.main before delegating, while retaining the implementation and
# tests in one orchestrator module.
base.IndustrialTechnologyOrchestrator.acquire_lease = _atomic_acquire_lease
IndustrialTechnologyOrchestrator = base.IndustrialTechnologyOrchestrator
main = base.main
