from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .connectors.data_gouv_ci import dataset_id_from_public_url
from .query import query_source_sql

if TYPE_CHECKING:
    from .engine import IvoireDataEngine


def _dataset_id(row: dict[str, Any]) -> str | None:
    for key in ("id", "slug", "name"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _title(row: dict[str, Any], fallback: str) -> str:
    for key in ("title", "name", "label", "description"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
    return fallback


def data_gouv_discoveries(engine: IvoireDataEngine, *, limit: int = 100) -> dict[str, Any]:
    """Compare the locally synchronized Data.gouv catalog with explicit registry mappings.

    This command does not ingest newly discovered datasets automatically. It is an audit
    surface: discovery -> rights/domain review -> registry/config -> sync.
    """
    limit = max(1, min(int(limit), 1000))
    try:
        catalog = query_source_sql(
            "civ_datagouv_catalog",
            "SELECT * FROM datagouv_catalog LIMIT 10000",
            settings=engine.settings,
            max_rows=10000,
        )
    except Exception as exc:
        return {
            "source_id": "civ_datagouv_catalog",
            "status": "CATALOG_UNAVAILABLE",
            "error": str(exc),
            "discovered_datasets": 0,
            "mapped_datasets": 0,
            "unmapped_datasets": 0,
            "rows": [],
        }

    discovered: dict[str, dict[str, Any]] = {}
    for row in catalog:
        dsid = _dataset_id(row)
        if not dsid:
            continue
        discovered[dsid] = {
            "dataset_id": dsid,
            "title": _title(row, dsid),
            "source_url": row.get("__ivoiredata_source_url") or f"https://data.gouv.ci/datasets/{dsid}",
            "primary_domain": row.get("__ivoiredata_primary_domain"),
            "classification_status": row.get("__ivoiredata_classification_status"),
        }

    mapped: dict[str, str] = {}
    unresolved_registry: list[str] = []
    for spec in engine.registry.all():
        if "data.gouv.ci" not in spec.source_url:
            continue
        dsid = dataset_id_from_public_url(spec.source_url)
        if dsid:
            mapped[dsid] = spec.source_id
        elif spec.source_id != "civ_datagouv_catalog":
            unresolved_registry.append(spec.source_id)

    rows = []
    for dsid, item in discovered.items():
        source_id = mapped.get(dsid)
        row = dict(item)
        row["registry_source_id"] = source_id
        row["status"] = "MAPPED" if source_id else "UNMAPPED"
        if not source_id:
            rows.append(row)

    rows.sort(key=lambda row: (str(row.get("primary_domain") or ""), str(row.get("title") or ""), row["dataset_id"]))
    return {
        "source_id": "civ_datagouv_catalog",
        "status": "OK",
        "discovered_datasets": len(discovered),
        "mapped_datasets": sum(1 for dsid in discovered if dsid in mapped),
        "unmapped_datasets": sum(1 for dsid in discovered if dsid not in mapped),
        "explicit_registry_mappings": len(mapped),
        "unresolved_registry_sources": sorted(unresolved_registry),
        "auto_ingest_new_discoveries": False,
        "review_workflow": "discover -> review domain/rights -> register/configure -> sync",
        "rows": rows[:limit],
        "truncated": len(rows) > limit,
    }
