from __future__ import annotations

from collections import Counter
from typing import Any

import requests

from .artifact_ledger import ArtifactLedger
from .connectors.data_gouv_ci_v2 import _dataset_id, _discover_official

_SOURCE_ID = "civ_datagouv_catalog"
_PHYSICAL = {"FETCHED", "VERIFIED", "UNCHANGED"}
_BAD = {"LOCAL_MISSING", "CORRUPTED", "FAILED"}


def data_gouv_coverage_from_catalog(engine: Any, catalog: list[dict[str, Any]]) -> dict[str, Any]:
    official_ids = sorted({dsid for meta in catalog if (dsid := _dataset_id(meta))})
    official_set = set(official_ids)
    ledger = ArtifactLedger(engine.settings.artifact_ledger_path)
    try:
        tracked_rows = [
            dict(row)
            for row in ledger.db.execute(
                "SELECT * FROM artifacts WHERE source_id=? ORDER BY artifact_id",
                (_SOURCE_ID,),
            ).fetchall()
        ]
    finally:
        ledger.close()

    by_id: dict[str, dict[str, Any]] = {}
    for row in tracked_rows:
        artifact_id = str(row.get("artifact_id") or "")
        if artifact_id.startswith("dataset:"):
            by_id[artifact_id[8:]] = row

    status_counts = Counter(str(row.get("status") or "DISCOVERED") for row in by_id.values())
    not_tracked = [dsid for dsid in official_ids if dsid not in by_id]
    local_missing = [dsid for dsid in official_ids if str(by_id.get(dsid, {}).get("status")) == "LOCAL_MISSING"]
    corrupted = [dsid for dsid in official_ids if str(by_id.get(dsid, {}).get("status")) == "CORRUPTED"]
    failed = [dsid for dsid in official_ids if str(by_id.get(dsid, {}).get("status")) == "FAILED"]
    verified = [dsid for dsid in official_ids if str(by_id.get(dsid, {}).get("status")) == "VERIFIED"]
    physical = [dsid for dsid in official_ids if str(by_id.get(dsid, {}).get("status")) in _PHYSICAL]
    unverified_physical = [
        dsid for dsid in physical if str(by_id.get(dsid, {}).get("status")) != "VERIFIED"
    ]
    unexpected = sorted(set(by_id) - official_set)
    bad_ids = sorted(set(local_missing + corrupted + failed))

    return {
        "source_id": _SOURCE_ID,
        "official_visible": len(official_ids),
        "ledger_tracked": sum(1 for dsid in official_ids if dsid in by_id),
        "physical": len(physical),
        "verified": len(verified),
        "unverified_physical": len(unverified_physical),
        "not_tracked": len(not_tracked),
        "bad": len(bad_ids),
        "by_status": dict(sorted(status_counts.items())),
        "complete_physical": len(not_tracked) == 0 and len(bad_ids) == 0 and len(physical) == len(official_ids),
        "complete_verified": len(verified) == len(official_ids) and len(official_ids) > 0,
        "missing_ids": not_tracked,
        "local_missing_ids": local_missing,
        "corrupted_ids": corrupted,
        "failed_ids": failed,
        "unverified_physical_ids": unverified_physical,
        "unexpected_tracked_ids": unexpected,
        "official_ids": official_ids,
    }


def data_gouv_coverage_audit(engine: Any) -> dict[str, Any]:
    """Compare the live official Data Fair catalogue to physical Artifact Ledger state."""
    session = requests.Session()
    session.headers.update({"User-Agent": engine.settings.user_agent, "Accept": "application/json"})
    catalog = _discover_official(session)
    payload = data_gouv_coverage_from_catalog(engine, catalog)
    payload["authority"] = "https://data.gouv.ci/data-fair/api/v1/datasets"
    payload["audit_mode"] = "LIVE_OFFICIAL_CATALOG_VS_PHYSICAL_LEDGER"
    return payload
