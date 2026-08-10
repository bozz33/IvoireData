#!/usr/bin/env python3
from __future__ import annotations

import csv
import sys
import urllib.parse
from pathlib import Path

REGISTRIES = [Path("registry/sources.csv"), Path("registry/ci_gold_completeness.csv")]
REQUIRED = {"source_id", "title", "domain", "provider", "source_url", "rights_tier", "access_tier", "priority"}
ALLOWED_PRIORITIES = {"P0", "P1", "P2"}
ALLOWED_RIGHT_PREFIXES = {"A_", "B_", "C_", "D_"}
errors: list[str] = []
seen: dict[str, str] = {}
count = 0

for path in REGISTRIES:
    if not path.exists():
        errors.append(f"missing registry file: {path}")
        continue
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for line_no, row in enumerate(rows, 2):
        count += 1
        prefix = f"{path}:{line_no}"
        missing = [key for key in REQUIRED if not (row.get(key) or "").strip()]
        if missing:
            errors.append(f"{prefix}: missing {missing}")
        source_id = (row.get("source_id") or "").strip()
        if source_id:
            if source_id in seen:
                errors.append(f"{prefix}: duplicate source_id {source_id} (already in {seen[source_id]})")
            else:
                seen[source_id] = str(path)
            if not source_id.startswith("civ_"):
                errors.append(f"{prefix}: source_id must use civ_ prefix")
        parsed = urllib.parse.urlsplit((row.get("source_url") or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append(f"{prefix}: invalid URL")
        priority = (row.get("priority") or "").strip().upper()
        if priority and priority not in ALLOWED_PRIORITIES:
            errors.append(f"{prefix}: invalid priority {priority}")
        rights = (row.get("rights_tier") or "").strip().upper()
        if rights and not any(rights.startswith(prefix_) for prefix_ in ALLOWED_RIGHT_PREFIXES):
            errors.append(f"{prefix}: invalid rights_tier {rights}")

print(f"{count} source records checked across {len(REGISTRIES)} registry files")
if errors:
    print("\n".join(errors)); sys.exit(1)
print("registry OK")
