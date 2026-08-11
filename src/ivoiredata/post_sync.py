from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .connectors.data_gouv_ci_v2 import _safe_table
from .delivery import source_paths
from .models import SourceSpec
from .settings import Settings
from .state_io import atomic_write_json
from .upstream_state import UpstreamState


def _archive_legacy_datagouv_orphans(settings: Settings, spec: SourceSpec) -> dict[str, Any]:
    paths = source_paths(settings, spec)
    tables_root = paths["tables"] / "data"
    if not tables_root.exists():
        return {"archived": 0, "tables": []}

    upstream = UpstreamState(settings.upstream_state_path)
    active_tables: set[str] = set()
    for row in upstream.source_rows(spec.source_id):
        artifact = str(row.get("artifact_id") or "")
        if not artifact.startswith("dataset:") or row.get("removed"):
            continue
        dsid = artifact.split(":", 1)[1]
        if dsid:
            active_tables.add(_safe_table(dsid))

    reserved = {"datagouv_catalog", "datagouv_sync_stats"}
    candidates = [
        path for path in tables_root.iterdir()
        if path.is_dir()
        and path.name.startswith("datagouv_")
        and path.name not in reserved
        and path.name not in active_tables
    ]
    if not candidates:
        return {"archived": 0, "tables": []}

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_root = paths["raw"] / "legacy" / "orphan_tables" / stamp
    archive_root.mkdir(parents=True, exist_ok=True)
    moved: list[dict[str, str]] = []
    for source in sorted(candidates):
        target = archive_root / source.name
        suffix = 1
        while target.exists():
            target = archive_root / f"{source.name}-{suffix}"
            suffix += 1
        shutil.move(str(source), str(target))
        moved.append({"table": source.name, "archive_path": str(target)})

    report = {"archived_at": stamp, "archived": len(moved), "tables": moved}
    atomic_write_json(archive_root / "archive.json", report)
    return report


def cleanup_after_success(settings: Settings, spec: SourceSpec) -> dict[str, Any] | None:
    """Run non-destructive source-specific migration cleanup after dlt commits."""
    if spec.connector == "data_gouv_ci" and spec.source_id == "civ_datagouv_catalog":
        return _archive_legacy_datagouv_orphans(settings, spec)
    return None
