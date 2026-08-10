from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .delivery import source_paths
from .state_io import load_json

if TYPE_CHECKING:
    from .engine import IvoireDataEngine
    from .models import SourceSpec

_COVERAGE_WEIGHTS = {
    "COVERED": 1.0,
    "PARTIAL": 0.55,
    "CONTROLLED": 0.75,
    "UNAVAILABLE": 0.50,
    "UNRESOLVED": 0.0,
    "MISSING": 0.0,
}
_PRIORITY_WEIGHTS = {"P0": 3.0, "P1": 2.0, "P2": 1.0}
_REQUIRED_MANIFEST_METADATA = (
    "country_code", "country_name", "source_id", "provider", "source_domain",
    "primary_domain", "language", "geographic_scope", "rights_tier",
    "access_tier", "classification_status",
)
_REQUIRED_DOCUMENT_COLUMNS = {
    "country_code", "country_name", "source_id", "provider", "source_domain",
    "primary_domain", "secondary_domains_json", "language", "document_type",
    "geographic_scope", "rights_tier", "access_tier", "classification_status",
    "classification_confidence", "retrieved_at", "content_type", "extraction_status",
}
_CRITICAL_CODES = {
    "MANIFEST_MISSING", "MANIFEST_SCHEMA_LEGACY", "EMPTY_DELIVERY",
    "SYNC_ERROR", "RIGHTS_METADATA_MISSING", "UPSTREAM_PARTIAL_FAILURE",
    "UPSTREAM_BACKLOG",
}
_SEVERITY_RANK = {"OK": 0, "ADVISORY": 1, "WARNING": 2, "CRITICAL": 3}
_STRUCTURED_STATS_FILES = {
    "civ_datagouv_catalog": "datagouv_sync_stats.json",
    "civ_ilostat": "ilostat_sync_stats.json",
    "civ_faostat": "faostat_sync_stats.json",
    "civ_worldbank_wdi": "worldbank_wdi_sync_stats.json",
    "civ_worldbank_projects": "worldbank_projects_sync_stats.json",
    "civ_uis": "uis_sync_stats.json",
}


def _read_json(path: Path) -> dict[str, Any]:
    data = load_json(path, {})
    return data if isinstance(data, dict) else {}


def _manifest(engine: IvoireDataEngine, spec: SourceSpec) -> dict[str, Any]:
    path = source_paths(engine.settings, spec)["manifest"]
    return _read_json(path) if path.exists() else {}


def _parquet_columns(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return set()
    columns: set[str] = set()
    for file in path.rglob("*.parquet"):
        if any(part.startswith("_dlt") for part in file.parts):
            continue
        try:
            schema = pq.ParquetFile(file).schema_arrow
        except Exception:
            continue
        columns.update(schema.names)
    return columns


def _zero_byte_files(root: Path) -> list[str]:
    rows: list[str] = []
    if not root.exists():
        return rows
    for file in root.rglob("*"):
        try:
            if file.is_file() and file.stat().st_size == 0:
                rows.append(str(file))
        except OSError:
            continue
    return rows


def _needs_ocr_files(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(str(path) for path in root.rglob("*.needs_ocr.json") if path.is_file())


def _issue_severity(spec: SourceSpec, issue: str) -> str:
    if issue == "DOCUMENTS_NEED_OCR":
        return "ADVISORY"
    if issue == "UPSTREAM_IGNORED_ITEMS":
        return "WARNING"
    if spec.priority.upper() == "P0" and issue in _CRITICAL_CODES:
        return "CRITICAL"
    return "WARNING"


def _structured_upstream_stats(paths: dict[str, Path], spec: SourceSpec) -> tuple[dict[str, Any], list[str]]:
    filename = _STRUCTURED_STATS_FILES.get(spec.source_id)
    if not filename:
        return {}, []
    stats = _read_json(paths["raw"] / filename)
    if not stats:
        return {}, []
    issues: list[str] = []
    if int(stats.get("failed") or 0) > 0:
        issues.append("UPSTREAM_PARTIAL_FAILURE")
    if int(stats.get("backlog_count") or 0) > 0 or int(stats.get("deferred_budget") or 0) > 0 or int(stats.get("skipped_oversize") or 0) > 0:
        issues.append("UPSTREAM_BACKLOG")
    ignored = stats.get("ignored_http400_indicators") or []
    if isinstance(ignored, list) and ignored:
        issues.append("UPSTREAM_IGNORED_ITEMS")
    return stats, issues


def coverage_audit(engine: IvoireDataEngine) -> dict[str, Any]:
    matrix = _read_json(engine.settings.ci_coverage_path)
    definitions = matrix.get("domains", []) if isinstance(matrix.get("domains"), list) else []
    all_specs = {spec.source_id: spec for spec in engine.registry.all()}
    audit_rows = {row["source_id"]: row for row in engine.audit(public_only=False)["rows"]}
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    weighted = 0.0
    weighted_max = 0.0

    for item in definitions:
        if not isinstance(item, dict):
            continue
        domain_id = str(item.get("domain_id") or "unknown")
        priority = str(item.get("priority") or "P1").upper()
        source_ids = [str(x) for x in item.get("source_ids", [])]
        minimum = max(1, int(item.get("minimum_usable_sources", 1)))
        registered = [sid for sid in source_ids if sid in all_specs]
        missing_registry = [sid for sid in source_ids if sid not in all_specs]
        enabled_public = [sid for sid in registered if all_specs[sid].enabled and all_specs[sid].public]
        controlled = [sid for sid in registered if not all_specs[sid].public]
        disabled = [sid for sid in registered if not all_specs[sid].enabled]
        usable = [sid for sid in enabled_public if audit_rows.get(sid, {}).get("delivery_status") not in {None, "", "EMPTY"}]
        empty_or_never = [sid for sid in enabled_public if audit_rows.get(sid, {}).get("delivery_status") in {None, "", "EMPTY"}]

        policy_status = str(item.get("policy_status") or "").upper()
        if policy_status in {"CONTROLLED", "UNAVAILABLE"}:
            status = policy_status
        elif missing_registry and not registered:
            status = "MISSING"
        elif disabled and not enabled_public and not controlled:
            status = "UNRESOLVED"
        elif controlled and not enabled_public:
            status = "CONTROLLED"
        elif len(usable) >= minimum and not missing_registry and not empty_or_never:
            status = "COVERED"
        elif usable or enabled_public or registered:
            status = "PARTIAL"
        else:
            status = "MISSING"

        counts[status] = counts.get(status, 0) + 1
        p_weight = _PRIORITY_WEIGHTS.get(priority, 1.0)
        weighted += _COVERAGE_WEIGHTS.get(status, 0.0) * p_weight
        weighted_max += p_weight
        rows.append({
            "domain_id": domain_id,
            "label": item.get("label") or domain_id,
            "priority": priority,
            "status": status,
            "minimum_usable_sources": minimum,
            "source_ids": source_ids,
            "registered_sources": registered,
            "usable_sources": usable,
            "empty_or_never_sources": empty_or_never,
            "disabled_sources": disabled,
            "controlled_sources": controlled,
            "missing_registry_sources": missing_registry,
            "notes": item.get("notes"),
        })

    p0_blockers = [row for row in rows if row["priority"] == "P0" and row["status"] in {"PARTIAL", "MISSING", "UNRESOLVED"}]
    score = 100.0 * weighted / weighted_max if weighted_max else 0.0
    return {
        "country_code": "CIV",
        "country_name": "Côte d'Ivoire",
        "matrix_version": matrix.get("version", 1),
        "summary": {
            "domains": len(rows),
            "status": dict(sorted(counts.items())),
            "coverage_score": round(score, 2),
            "p0_blockers": len(p0_blockers),
        },
        "p0_blockers": p0_blockers,
        "rows": rows,
    }


def quality_audit(engine: IvoireDataEngine) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    active = engine.registry.list(public_only=True)
    audit_map = {row["source_id"]: row for row in engine.audit(public_only=True)["rows"]}
    document_sources = 0
    document_schema_complete = 0
    total_needs_ocr = 0
    total_zero_byte = 0
    legacy_manifests = 0
    partial_upstreams = 0
    backlog_upstreams = 0

    for spec in active:
        paths = source_paths(engine.settings, spec)
        manifest = _manifest(engine, spec)
        metadata = manifest.get("metadata", {}) if isinstance(manifest.get("metadata"), dict) else {}
        missing_meta = [key for key in _REQUIRED_MANIFEST_METADATA if not metadata.get(key)]
        audit = audit_map.get(spec.source_id, {})
        delivery_status = str(audit.get("delivery_status") or "EMPTY")
        sync_status = str(audit.get("sync_status") or "NEVER")

        source_issues: list[str] = []
        if not manifest:
            source_issues.append("MANIFEST_MISSING")
        elif int(manifest.get("schema_version") or 0) < 3:
            source_issues.append("MANIFEST_SCHEMA_LEGACY")
            legacy_manifests += 1
        if missing_meta:
            source_issues.append("MANIFEST_METADATA_INCOMPLETE")
        if delivery_status == "EMPTY":
            source_issues.append("EMPTY_DELIVERY")
        if sync_status == "ERROR":
            source_issues.append("SYNC_ERROR")
        if not spec.rights_tier or not spec.access_tier:
            source_issues.append("RIGHTS_METADATA_MISSING")

        upstream_stats, upstream_issues = _structured_upstream_stats(paths, spec)
        source_issues.extend(upstream_issues)
        if "UPSTREAM_PARTIAL_FAILURE" in upstream_issues:
            partial_upstreams += 1
        if "UPSTREAM_BACKLOG" in upstream_issues:
            backlog_upstreams += 1

        zero_byte = _zero_byte_files(paths["root"])
        needs_ocr = _needs_ocr_files(paths["documents"])
        total_zero_byte += len(zero_byte)
        total_needs_ocr += len(needs_ocr)
        if zero_byte:
            source_issues.append("ZERO_BYTE_FILES")
        if needs_ocr:
            source_issues.append("DOCUMENTS_NEED_OCR")

        missing_columns: list[str] = []
        if spec.connector == "public_web" and delivery_status != "EMPTY":
            document_sources += 1
            columns = _parquet_columns(paths["tables"])
            missing_columns = sorted(_REQUIRED_DOCUMENT_COLUMNS - columns)
            if not missing_columns:
                document_schema_complete += 1
            else:
                source_issues.append("DOCUMENT_METADATA_COLUMNS_INCOMPLETE")

        source_severities: list[str] = []
        for issue in dict.fromkeys(source_issues):
            severity = _issue_severity(spec, issue)
            source_severities.append(severity)
            issues.append({
                "source_id": spec.source_id,
                "priority": spec.priority,
                "severity": severity,
                "issue": issue,
            })
        row_severity = max(source_severities, key=lambda value: _SEVERITY_RANK[value]) if source_severities else "OK"

        rows.append({
            "source_id": spec.source_id,
            "priority": spec.priority,
            "connector": spec.connector,
            "delivery_status": delivery_status,
            "sync_status": sync_status,
            "manifest_schema_version": int(manifest.get("schema_version") or 0) if manifest else None,
            "missing_manifest_metadata": missing_meta,
            "missing_document_columns": missing_columns,
            "needs_ocr_documents": len(needs_ocr),
            "needs_ocr_sidecars": needs_ocr[:20],
            "zero_byte_files": len(zero_byte),
            "upstream_stats": upstream_stats,
            "severity": row_severity,
        })

    critical_count = sum(1 for issue in issues if issue["severity"] == "CRITICAL")
    warning_count = sum(1 for issue in issues if issue["severity"] == "WARNING")
    advisory_count = sum(1 for issue in issues if issue["severity"] == "ADVISORY")
    schema_ratio = (document_schema_complete / document_sources) if document_sources else 1.0
    return {
        "country_code": "CIV",
        "summary": {
            "active_public_sources": len(active),
            "critical_issues": critical_count,
            "warnings": warning_count,
            "advisories": advisory_count,
            "legacy_manifests": legacy_manifests,
            "zero_byte_files": total_zero_byte,
            "needs_ocr_documents": total_needs_ocr,
            "automatic_ocr": False,
            "document_sources": document_sources,
            "document_schema_complete": document_schema_complete,
            "document_schema_completeness_pct": round(schema_ratio * 100, 2),
            "structured_partial_failure_sources": partial_upstreams,
            "structured_backlog_sources": backlog_upstreams,
            "passed": critical_count == 0,
        },
        "issues": issues,
        "rows": rows,
    }


def ci_gold_report(engine: IvoireDataEngine) -> dict[str, Any]:
    base_audit = engine.audit(public_only=True)
    coverage = coverage_audit(engine)
    quality = quality_audit(engine)
    qualification = engine.qualification.status()
    audit_rows = base_audit["rows"]

    active = len(audit_rows)
    public_specs = engine.registry.list(public_only=True)
    automatic_ids = {spec.source_id for spec in engine.registry.list(public_only=True, auto_only=True)}
    attempted_ids = set(qualification.get("sources_attempted") or [])
    baseline_ids = set(qualification.get("baseline_sources") or [])
    covered_automatic_ids = attempted_ids | baseline_ids
    automatic_sources_exercised = bool(automatic_ids) and automatic_ids.issubset(covered_automatic_ids)
    missing_automatic_exercise = sorted(automatic_ids - covered_automatic_ids)

    non_error = sum(1 for row in audit_rows if row["sync_status"] != "ERROR")
    non_empty = sum(1 for row in audit_rows if row["delivery_status"] != "EMPTY")
    freshish = sum(1 for row in audit_rows if row["freshness_status"] in {"FRESH", "DUE"})
    rights_ok = sum(1 for spec in public_specs if spec.rights_tier and spec.access_tier)
    manifests_ok = sum(1 for spec in public_specs if source_paths(engine.settings, spec)["manifest"].exists())
    catalog_ok = (engine.settings.data_dir / "catalog.json").exists()

    coverage_component = coverage["summary"]["coverage_score"]
    quality_component = max(0.0, 100.0 - quality["summary"]["critical_issues"] * 10.0 - quality["summary"]["warnings"] * 1.5)
    classification_component = float(quality["summary"]["document_schema_completeness_pct"])
    freshness_component = (freshish / active * 100.0) if active else 0.0
    stability_fully_qualified = bool(qualification["qualified"] and automatic_sources_exercised)
    stability_component = 100.0 if stability_fully_qualified else min(
        90.0,
        min(qualification["elapsed_days"] / 14.0, 1.0) * 45.0
        + min(qualification["cycles_total"] / 14.0, 1.0) * 30.0
        + (15.0 if automatic_sources_exercised else 0.0)
        - min(qualification["cycles_with_errors"] * 10.0, 40.0),
    )
    rights_component = (rights_ok / active * 100.0) if active else 0.0
    handoff_component = ((manifests_ok / active) * 80.0 + (20.0 if catalog_ok else 0.0)) if active else 0.0

    components = {
        "coverage": round(coverage_component, 2),
        "quality_provenance": round(quality_component, 2),
        "classification": round(classification_component, 2),
        "freshness": round(freshness_component, 2),
        "stability": round(max(0.0, stability_component), 2),
        "rights": round(rights_component, 2),
        "handoff": round(handoff_component, 2),
    }
    weights = {
        "coverage": 0.25,
        "quality_provenance": 0.20,
        "classification": 0.15,
        "freshness": 0.15,
        "stability": 0.10,
        "rights": 0.10,
        "handoff": 0.05,
    }
    score = sum(components[key] * weights[key] for key in weights)

    gates = {
        "score_at_least_95": score >= 95.0,
        "no_p0_coverage_blocker": coverage["summary"]["p0_blockers"] == 0,
        "no_critical_quality_issue": quality["summary"]["critical_issues"] == 0,
        "structured_upstreams_complete": quality["summary"]["structured_partial_failure_sources"] == 0 and quality["summary"]["structured_backlog_sources"] == 0,
        "no_active_empty": non_empty == active,
        "no_active_sync_error": non_error == active,
        "rights_complete": rights_ok == active,
        "document_metadata_complete": quality["summary"]["document_schema_completeness_pct"] >= 99.0,
        "manifest_v3_complete": quality["summary"]["legacy_manifests"] == 0,
        "qualification_14_days": qualification["qualified"],
        "automatic_sources_exercised": automatic_sources_exercised,
        "catalog_present": catalog_ok,
        "all_manifests_present": manifests_ok == active,
    }
    approved = all(gates.values())
    return {
        "country_code": "CIV",
        "country_name": "Côte d'Ivoire",
        "release_target": "CI_GOLD",
        "score": round(score, 2),
        "approved": approved,
        "components": components,
        "gates": gates,
        "stability_coverage": {
            "automatic_sources": len(automatic_ids),
            "baseline_sources": len(automatic_ids & baseline_ids),
            "automatic_sources_attempted_in_window": len(automatic_ids & attempted_ids),
            "automatic_sources_exercised": len(automatic_ids & covered_automatic_ids),
            "missing_automatic_sources": missing_automatic_exercise,
        },
        "coverage": coverage,
        "quality": quality,
        "qualification": qualification,
        "audit_summary": base_audit["summary"],
    }


def write_ci_gold_report(engine: IvoireDataEngine) -> dict[str, Any]:
    report = ci_gold_report(engine)
    root = engine.settings.data_dir / "reports" / "ci-gold"
    root.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "ci-gold-report.json": report,
        "coverage.json": report["coverage"],
        "quality.json": report["quality"],
        "qualification.json": report["qualification"],
        "audit.json": engine.audit(public_only=True),
        "upstreams.json": engine.upstream_audit(),
        "sources.json": [spec.__dict__ for spec in engine.registry.all()],
    }
    for name, payload in artifacts.items():
        (root / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    gates = "\n".join(f"- [{'x' if ok else ' '}] {name}" for name, ok in report["gates"].items())
    markdown = (
        "# IvoireData — CI Gold qualification\n\n"
        f"- Score: **{report['score']}/100**\n"
        f"- Approved: **{'YES' if report['approved'] else 'NO'}**\n"
        f"- Coverage: **{report['coverage']['summary']['coverage_score']}/100**\n"
        f"- Qualification elapsed: **{report['qualification']['elapsed_days']} days**\n"
        f"- Automatic sources covered: **{report['stability_coverage']['automatic_sources_exercised']}/{report['stability_coverage']['automatic_sources']}**\n"
        f"- Baseline AUTO sources: **{report['stability_coverage']['baseline_sources']}**\n"
        f"- Structured upstream partial failures: **{report['quality']['summary']['structured_partial_failure_sources']}**\n"
        f"- Structured upstream backlogs: **{report['quality']['summary']['structured_backlog_sources']}**\n"
        f"- NEEDS_OCR documents: **{report['quality']['summary']['needs_ocr_documents']}** (advisory)\n\n"
        "## Gates\n\n" + gates + "\n"
    )
    (root / "ci-gold-report.md").write_text(markdown, encoding="utf-8")
    return {"report": report, "output_dir": str(root)}
