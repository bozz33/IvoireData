from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import SourceSpec
from .settings import Settings

_SAFE = re.compile(r"[^a-zA-Z0-9_.-]+")


def safe_name(value: str) -> str:
    value = _SAFE.sub("_", (value or "unknown").strip()).strip("._")
    return value or "unknown"


def source_root(settings: Settings, spec: SourceSpec) -> Path:
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


def write_source_manifest(
    settings: Settings,
    spec: SourceSpec,
    *,
    status: str,
    connector: str,
    started_at: str,
    finished_at: str,
    details: str = "",
) -> dict[str, Any]:
    paths = ensure_source_layout(settings, spec)
    manifest = {
        "schema_version": 1,
        "source_id": spec.source_id,
        "title": spec.title,
        "domain": spec.domain,
        "provider": spec.provider,
        "source_url": spec.source_url,
        "rights_tier": spec.rights_tier,
        "access_tier": spec.access_tier,
        "priority": spec.priority,
        "connector": connector,
        "auto_sync": spec.auto_sync,
        "refresh_hours": spec.refresh_hours,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "paths": {
            "tables": "tables/",
            "raw": "raw/",
            "documents": "documents/",
        },
        "inventory": {
            "tables": _dir_stats(paths["tables"]),
            "raw": _dir_stats(paths["raw"]),
            "documents": _dir_stats(paths["documents"]),
        },
        "details": details[-4000:],
    }
    paths["manifest"].write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def rebuild_catalog(settings: Settings, specs: list[SourceSpec]) -> dict[str, Any]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
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
            item = {
                "source_id": spec.source_id,
                "title": spec.title,
                "domain": spec.domain,
                "provider": spec.provider,
                "source_url": spec.source_url,
                "status": "not_synced",
                "auto_sync": spec.auto_sync,
                "refresh_hours": spec.refresh_hours,
            }
        sources.append(item)
        domains.setdefault(spec.domain, []).append(item)
    catalog = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "storage": "local",
        "root": str(settings.data_dir),
        "domains": {name: rows for name, rows in sorted(domains.items())},
        "sources": sources,
    }
    (settings.data_dir / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    return catalog


def inventory(settings: Settings, specs: list[SourceSpec]) -> dict[str, Any]:
    catalog_path = settings.data_dir / "catalog.json"
    if not catalog_path.exists():
        return rebuild_catalog(settings, specs)
    try:
        return json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return rebuild_catalog(settings, specs)
