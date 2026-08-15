from __future__ import annotations

from typing import Any

from .artifact_ledger import ArtifactLedger
from .engine import IvoireDataEngine


_ORIGINAL_SYNC = IvoireDataEngine.sync


def ingest_existing_upstreams(engine: IvoireDataEngine, source_id: str | None = None) -> int:
    ledger = ArtifactLedger(engine.settings.artifact_ledger_path)
    try:
        source_ids = [source_id] if source_id else [spec.source_id for spec in engine.registry.all()]
        count = 0
        for sid in source_ids:
            count += ledger.ingest_upstream_rows(engine.upstreams.source_rows(sid))
        return count
    finally:
        ledger.close()


def _sync_with_artifact_ledger(self: IvoireDataEngine, source_id: str, *, force: bool = False):
    spec = self.registry.get(source_id)
    ledger = ArtifactLedger(self.settings.artifact_ledger_path)
    run_id = ledger.start_run(source_id, connector=spec.connector, force=force)
    try:
        result = _ORIGINAL_SYNC(self, source_id, force=force)
        ledger.ingest_upstream_rows(self.upstreams.source_rows(source_id), run_id=run_id)
        ledger.finish_run(
            run_id,
            status=str(result.status or "UNKNOWN").upper(),
            error=result.details if str(result.status).lower() != "success" else None,
        )
        return result
    except BaseException as exc:
        # Keep the run ledger useful even for interrupts/system-level failures that
        # happen outside the engine's normal exception-to-SyncResult path.
        try:
            ledger.ingest_upstream_rows(self.upstreams.source_rows(source_id), run_id=run_id)
            ledger.finish_run(run_id, status="ABORTED", error=str(exc))
        finally:
            ledger.close()
        raise
    finally:
        # finish_run commits before close; close is idempotent for the normal path.
        try:
            ledger.close()
        except Exception:
            pass


if not getattr(IvoireDataEngine.sync, "__ivoiredata_artifact_ledger__", False):
    setattr(_sync_with_artifact_ledger, "__ivoiredata_artifact_ledger__", True)
    IvoireDataEngine.sync = _sync_with_artifact_ledger  # type: ignore[method-assign]
