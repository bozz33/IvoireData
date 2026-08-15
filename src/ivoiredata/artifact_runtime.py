from __future__ import annotations

from .artifact_ledger import ArtifactLedger
from .engine import IvoireDataEngine
from .http_client import http_run_context


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
    http_ctx = http_run_context(
        source_id=source_id,
        run_id=run_id,
        state_dir=self.settings.state_dir,
        user_agent=self.settings.user_agent,
        options=spec.options,
    )
    try:
        with http_ctx:
            result = _ORIGINAL_SYNC(self, source_id, force=force)
        http_metrics = http_ctx.snapshot()
        ledger.ingest_upstream_rows(self.upstreams.source_rows(source_id), run_id=run_id)
        budget_exceeded = bool(http_metrics.get("budget_exceeded"))
        run_status = "PARTIAL_BUDGET" if budget_exceeded else str(result.status or "UNKNOWN").upper()
        run_error = (
            str(http_metrics.get("budget_reason") or result.details)
            if budget_exceeded or str(result.status).lower() != "success"
            else None
        )
        ledger.finish_run(
            run_id,
            status=run_status,
            error=run_error,
            http_metrics=http_metrics,
        )
        return result
    except BaseException as exc:
        # Keep both ledgers useful even for interrupts/system-level failures outside the
        # engine's normal exception-to-SyncResult path.
        http_metrics = http_ctx.snapshot()
        try:
            ledger.ingest_upstream_rows(self.upstreams.source_rows(source_id), run_id=run_id)
            ledger.finish_run(
                run_id,
                status="PARTIAL_BUDGET" if http_metrics.get("budget_exceeded") else "ABORTED",
                error=str(http_metrics.get("budget_reason") or exc),
                http_metrics=http_metrics,
            )
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
