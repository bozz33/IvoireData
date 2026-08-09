#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

from ivoiredata.registry import SourceRegistry


ALLOWED_CONNECTORS = {
    "data_gouv_ci", "http_file", "public_web", "world_bank_wdi", "world_bank_projects",
    "geoboundaries", "ilostat_ref_area", "osm_geofabrik", "bulk_catalog",
}

registry_path = Path("registry/sources.csv")
runtime_path = Path("configs/runtime_sources.json")
config = json.loads(runtime_path.read_text(encoding="utf-8"))
registry = SourceRegistry.load(registry_path, runtime_path)
known = {spec.source_id for spec in registry.list()}
errors: list[str] = []

for source_id in config.get("sources", {}):
    if source_id not in known:
        errors.append(f"runtime config references unknown source: {source_id}")

for spec in registry.list():
    if spec.connector not in ALLOWED_CONNECTORS:
        errors.append(f"{spec.source_id}: unsupported connector {spec.connector}")
    if spec.auto_sync and not spec.public:
        errors.append(f"{spec.source_id}: auto_sync enabled but unattended ingestion policy blocks this source")
    if spec.refresh_hours <= 0:
        errors.append(f"{spec.source_id}: refresh_hours must be > 0")

print(f"{len(known)} sources checked; {len(config.get('sources', {}))} runtime overrides")
if errors:
    print("\n".join(errors))
    sys.exit(1)
print("runtime config OK")
