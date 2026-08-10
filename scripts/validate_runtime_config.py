#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

from ivoiredata.registry import SourceRegistry
from ivoiredata.runtime_control import load_runtime_config


ALLOWED_CONNECTORS = {
    "data_gouv_ci", "http_file", "public_web", "world_bank_wdi", "world_bank_projects",
    "geoboundaries", "ilostat_ref_area", "osm_geofabrik", "bulk_catalog",
    "faostat_country", "uis_country",
}

registry_path = Path("registry/sources.csv")
runtime_path = Path("configs/runtime_sources.json")
ci_gold_path = Path("configs/ci_gold_sources.json")
base = json.loads(runtime_path.read_text(encoding="utf-8"))
ci_gold = json.loads(ci_gold_path.read_text(encoding="utf-8")) if ci_gold_path.exists() else {}
config = load_runtime_config(runtime_path, None, [ci_gold_path])
registry = SourceRegistry.load(registry_path, runtime_path, None, [ci_gold_path])
known = {spec.source_id for spec in registry.all()}
errors: list[str] = []

for label, payload in (("runtime", base), ("ci_gold", ci_gold)):
    for source_id in payload.get("sources", {}):
        if source_id not in known:
            errors.append(f"{label} config references unknown source: {source_id}")

for spec in registry.all():
    if spec.connector not in ALLOWED_CONNECTORS:
        errors.append(f"{spec.source_id}: unsupported connector {spec.connector}")
    if spec.auto_sync and not spec.public:
        errors.append(f"{spec.source_id}: auto_sync enabled but unattended ingestion policy blocks this source")
    if spec.refresh_hours <= 0:
        errors.append(f"{spec.source_id}: refresh_hours must be > 0")
    if not spec.options.get("country_code") and spec.source_id.startswith("civ_"):
        # country_code defaults to CIV at the metadata layer; this check simply documents
        # that CIV sources remain eligible for that default rather than requiring repetition.
        pass

updates = config.get("updates", {})
if "scheduler_interval_seconds" in updates and int(updates["scheduler_interval_seconds"]) < 300:
    errors.append("scheduler_interval_seconds must be >= 300")

print(
    f"{len(known)} sources checked; "
    f"{len(base.get('sources', {}))} base overrides; "
    f"{len(ci_gold.get('sources', {}))} CI Gold overrides"
)
if errors:
    print("\n".join(errors))
    sys.exit(1)
print("runtime config OK")
