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
    "faostat_country", "uis_country", "official_docs",
}
ALLOWED_COVERAGE_STATUS = {"CONTROLLED", "UNAVAILABLE", ""}

registry_path = Path("registry/sources.csv")
registry_overlay = Path("registry/ci_gold_completeness.csv")
programming_registry = Path("registry/programming_docs.csv")
runtime_path = Path("configs/runtime_sources.json")
ci_gold_path = Path("configs/ci_gold_sources.json")
programming_path = Path("configs/programming_docs_sources.json")
coverage_path = Path("configs/ci_coverage.json")

base = json.loads(runtime_path.read_text(encoding="utf-8"))
ci_gold = json.loads(ci_gold_path.read_text(encoding="utf-8")) if ci_gold_path.exists() else {}
programming = json.loads(programming_path.read_text(encoding="utf-8")) if programming_path.exists() else {}
programming_expanded = load_runtime_config(programming_path) if programming_path.exists() else {}
coverage = json.loads(coverage_path.read_text(encoding="utf-8")) if coverage_path.exists() else {}

runtime_overlays = [path for path in (ci_gold_path, programming_path) if path.exists()]
registry_overlays = [path for path in (registry_overlay, programming_registry) if path.exists()]
config = load_runtime_config(runtime_path, None, runtime_overlays)
registry = SourceRegistry.load(registry_path, runtime_path, None, runtime_overlays, registry_overlays)
known = {spec.source_id for spec in registry.all()}
errors: list[str] = []

for label, payload in (("runtime", base), ("ci_gold", ci_gold), ("programming_docs", programming_expanded)):
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
    if spec.connector == "official_docs":
        language = str(spec.options.get("programming_language") or "").strip()
        if not language:
            errors.append(f"{spec.source_id}: official_docs source missing programming_language")
        if int(spec.options.get("max_pages") or 0) < 1:
            errors.append(f"{spec.source_id}: official_docs max_pages must be >= 1")
        if int(spec.options.get("max_new_bytes_per_run") or 0) < 1:
            errors.append(f"{spec.source_id}: official_docs max_new_bytes_per_run must be >= 1")

seen_domains: set[str] = set()
for row in coverage.get("domains", []):
    domain_id = str(row.get("domain_id") or "").strip()
    if not domain_id:
        errors.append("coverage row missing domain_id")
        continue
    if domain_id in seen_domains:
        errors.append(f"duplicate coverage domain_id: {domain_id}")
    seen_domains.add(domain_id)
    priority = str(row.get("priority") or "").upper()
    if priority not in {"P0", "P1", "P2"}:
        errors.append(f"coverage {domain_id}: invalid priority {priority}")
    policy_status = str(row.get("policy_status") or "").upper()
    if policy_status not in ALLOWED_COVERAGE_STATUS:
        errors.append(f"coverage {domain_id}: invalid policy_status {policy_status}")
    minimum = int(row.get("minimum_usable_sources", 1))
    if minimum < 1:
        errors.append(f"coverage {domain_id}: minimum_usable_sources must be >= 1")
    for source_id in row.get("source_ids", []):
        if source_id not in known:
            errors.append(f"coverage {domain_id} references unknown source: {source_id}")

updates = config.get("updates", {})
if "scheduler_interval_seconds" in updates and int(updates["scheduler_interval_seconds"]) < 300:
    errors.append("scheduler_interval_seconds must be >= 300")

print(
    f"{len(known)} sources checked; "
    f"{len(base.get('sources', {}))} base overrides; "
    f"{len(ci_gold.get('sources', {}))} CI Gold overrides; "
    f"{len(programming_expanded.get('sources', {}))} programming-doc overrides; "
    f"{len(seen_domains)} coverage domains"
)
if errors:
    print("\n".join(errors)); sys.exit(1)
print("runtime config, CI Gold coverage and programming documentation overlays OK")
