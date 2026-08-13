from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .locks import file_lock
from .metadata import source_metadata
from .models import SourceSpec
from .settings import Settings
from .state_io import atomic_write_json

_SAFE = re.compile(r"[^a-zA-Z0-9_.-]+")


def safe_name(value: str) -> str:
    value = _SAFE.sub("_", (value or "unknown").strip()).strip("._")
    return value or "unknown"


def source_root(settings: Settings, spec: SourceSpec) -> Path:
    if spec.connector == "official_docs":
        language = safe_name(str(spec.options.get("programming_language") or "general").casefold())
        return settings.data_dir / "programming_docs" / language / safe_name(spec.source_id)
    return settings.data_dir / "domains" / safe_name(spec.domain) / safe_name(spec.source_id)


def source_paths(settings: Settings, spec: SourceSpec) -> dict[str, Path]:
    root = source_root(settings, spec)
    return {
        "root": root,
        "tables": root / "tables",
        "raw": root / "raw",
        "documents": root / "documents",
        "manifest": root / "manifest.json",
    }


def ensure_source_layout(settings: Settings, spec: SourceSpec) -> dict[str, Path]:
    paths = source_paths(settings, spec)
    for key in ("root", "tables", "raw", "documents"):
        paths[key].mkdir(parents=True, exist_ok=True)
    return paths


def _dir_stats(path: Path) -> dict[str, int]:
    files = 0
    bytes_total = 0
    if path.exists():
        for item in path.rglob("*"):
            if item.is_file():
                files += 1
                try:
                    bytes_total += item.stat().st_size
                except OSError:
                    pass
    return {"files": files, "bytes": bytes_total}


def _parquet_stats(path: Path) -> dict[str, int]:
    """Read Parquet metadata only; never scan full table contents."""
    files = 0
    bytes_total = 0
    rows = 0
    if not path.exists():
        return {"files": 0, "bytes": 0, "rows": 0}
    try:
        import pyarrow.parquet as pq
    except ImportError:
        base = _dir_stats(path)
        return {**base, "rows": 0}
    for item in path.rglob("*.parquet"):
        if any(part.startswith("_dlt") for part in item.parts):
            continue
        files += 1
        try:
            bytes_total += item.stat().st_size
            rows += int(pq.ParquetFile(item).metadata.num_rows)
        except (OSError, ValueError, TypeError):
            continue
    return {"files": files, "bytes": bytes_total, "rows": rows}


def _transport_security(spec: SourceSpec) -> str:
    if spec.source_url.lower().startswith("http://"):
        return "HTTP"
    if spec.options.get("verify_ssl") is False:
        return "DEGRADED_TLS"
    return "VERIFIED_TLS"


_STRUCTURED_CONNECTORS = frozenset({
    "data_gouv_ci",
    "world_bank_wdi",
    "world_bank_projects",
    "ilostat_ref_area",
    "faostat_country",
    "uis_country",
    "geoboundaries",
    "http_file",
})


def compute_delivery_status(
    spec: SourceSpec,
    *,
    sync_status: str,
    inventory: dict[str, dict[str, int]],
) -> tuple[str, list[str]]:
    tables = inventory["tables"]
    raw = inventory["raw"]
    documents = inventory["documents"]
    rows = int(tables.get("rows", 0))
    warnings: list[str] = []

    is_structured = spec.connector in _STRUCTURED_CONNECTORS

    if spec.options.get("metadata_only"):
        delivery = "METADATA_ONLY" if (rows or documents["files"] or raw["files"]) else "EMPTY"
        if delivery != "EMPTY":
            warnings.append("METADATA_ONLY_SOURCE")
    elif spec.connector == "osm_geofabrik":
        delivery = "SNAPSHOT_ONLY" if raw["files"] > 0 else "EMPTY"
    elif spec.connector in {"public_web", "official_docs"}:
        if rows > 0 or documents["files"] > 0 or raw["files"] > 0:
            delivery = "DOCUMENTS_ONLY"
        else:
            delivery = "EMPTY"
    elif is_structured and rows > 0:
        delivery = "FULL_STRUCTURED"
    elif documents["files"] > 0:
        delivery = "DOCUMENTS_ONLY"
    elif raw["files"] > 0:
        delivery = "SNAPSHOT_ONLY"
    elif rows > 0:
        delivery = "FULL_STRUCTURED"
    else:
        delivery = "EMPTY"

    if sync_status == "success" and delivery == "EMPTY":
        warnings.append("EMPTY_AFTER_SUCCESS")
    if sync_status == "error" and delivery != "EMPTY":
        warnings.append("SYNC_ERROR_WITH_STALE_DATA")
    if _transport_security(spec) == "DEGRADED_TLS":
        warnings.append("TLS_VERIFICATION_DISABLED")
    return delivery, warnings


def _freshness_status(sync_status: str, freshness_state: dict[str, Any] | None, *, due: bool) -> str:
    state = freshness_state or {}
    last_success = state.get("last_success")
    if sync_status == "error":
        return "STALE" if last_success else "NEVER_SYNCED"
    if not last_success:
        return "NEVER_SYNCED"
    return "DUE" if due else "FRESH"


def write_source_manifest(
    settings: Settings,
    spec: SourceSpec,
    *,
    status: str,
    connector: str,
    started_at: str,
    finished_at: str,
    details: str = "",
    freshness_state: dict[str, Any] | None = None,
    due: bool = False,
) -> dict[str, Any]:
    paths = ensure_source_layout(settings, spec)
    inv = {
        "tables": _parquet_stats(paths["tables"]),
        "raw": _dir_stats(paths["raw"]),
        "documents": _dir_stats(paths["documents"]),
    }
    delivery_status, warnings = compute_delivery_status(spec, sync_status=status, inventory=inv)
    freshness_status = _freshness_status(status, freshness_state, due=due)
    transport_security = _transport_security(spec)
    state = freshness_state or {}
    meta = source_metadata(spec)

    manifest = {
        "schema_version": 3,
        "source_id": spec.source_id,
        "title": spec.title,
        "domain": spec.domain,
        "country_code": meta["country_code"],
        "country_name": meta["country_name"],
        "provider": spec.provider,
        "source_url": spec.source_url,
        "rights_tier": spec.rights_tier,
        "access_tier": spec.access_tier,
        "priority": spec.priority,
        "connector": connector,
        "auto_sync": spec.auto_sync,
        "refresh_hours": spec.refresh_hours,
        "status": status,
        "delivery_status": delivery_status,
        "freshness_status": freshness_status,
        "transport_security": transport_security,
        "started_at": started_at,
        "finished_at": finished_at,
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "metadata": meta,
        "sync": {
            "status": status.upper(),
            "started_at": started_at,
            "finished_at": finished_at,
            "last_attempt": state.get("last_attempt"),
            "last_success": state.get("last_success"),
        },
        "delivery": {
            "status": delivery_status,
            "rows": int(inv["tables"].get("rows", 0)),
            "table_files": int(inv["tables"].get("files", 0)),
            "table_bytes": int(inv["tables"].get("bytes", 0)),
            "raw_files": int(inv["raw"].get("files", 0)),
            "raw_bytes": int(inv["raw"].get("bytes", 0)),
            "document_files": int(inv["documents"].get("files", 0)),
            "document_bytes": int(inv["documents"].get("bytes", 0)),
        },
        "freshness": {
            "status": freshness_status,
            "refresh_hours": spec.refresh_hours,
            "due": bool(due),
            "last_success": state.get("last_success"),
        },
        "transport": {"security": transport_security},
        "rights": {"tier": spec.rights_tier, "access": spec.access_tier},
        "warnings": warnings,
        "paths": {"tables": "tables/", "raw": "raw/", "documents": "documents/"},
        "inventory": inv,
        "details": details[-4000:],
    }
    atomic_write_json(paths["manifest"], manifest)
    return manifest


def _build_catalog(settings: Settings, specs: list[SourceSpec]) -> dict[str, Any]:
    domains: dict[str, list[dict[str, Any]]] = {}
    sources: list[dict[str, Any]] = []
    for spec in specs:
        paths = source_paths(settings, spec)
        if paths["manifest"].exists():
            try:
                item = json.loads(paths["manifest"].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                item = {"source_id": spec.source_id, "domain": spec.domain, "status": "manifest_error"}
        else:
            meta = source_metadata(spec)
            item = {
                "source_id": spec.source_id,
                "title": spec.title,
                "domain": spec.domain,
                "country_code": meta["country_code"],
                "country_name": meta["country_name"],
                "provider": spec.provider,
                "source_url": spec.source_url,
                "status": "not_synced",
                "delivery_status": "EMPTY",
                "freshness_status": "NEVER_SYNCED",
                "auto_sync": spec.auto_sync,
                "refresh_hours": spec.refresh_hours,
                "metadata": meta,
            }
        sources.append(item)
        domains.setdefault(spec.domain, []).append(item)
    country_codes = sorted({str(source.get("country_code") or "") for source in sources if source.get("country_code")})
    return {
        "schema_version": 3,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "storage": "local",
        "scope": "MIXED" if len(country_codes) > 1 else (country_codes[0] if country_codes else "UNKNOWN"),
        "country_codes": country_codes,
        "root": str(settings.data_dir),
        "domains": {name: rows for name, rows in sorted(domains.items())},
        "domain_index": {name: [row.get("source_id") for row in rows] for name, rows in sorted(domains.items())},
        "sources": sources,
    }


def rebuild_catalog(settings: Settings, specs: list[SourceSpec]) -> dict[str, Any]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    lock = settings.state_dir / "locks" / "catalog.lock"
    with file_lock(lock, timeout=120):
        catalog = _build_catalog(settings, specs)
        atomic_write_json(settings.data_dir / "catalog.json", catalog)
        return catalog


def inventory(settings: Settings, specs: list[SourceSpec]) -> dict[str, Any]:
    catalog_path = settings.data_dir / "catalog.json"
    if not catalog_path.exists():
        return rebuild_catalog(settings, specs)
    try:
        return json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return rebuild_catalog(settings, specs)
