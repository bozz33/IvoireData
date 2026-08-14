from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .delivery import source_paths
from .state_io import atomic_write_json, load_json

if TYPE_CHECKING:
    from .engine import IvoireDataEngine


def programming_specs(engine: IvoireDataEngine):
    return [spec for spec in engine.registry.list() if spec.connector == "official_docs"]


def programming_docs_audit(engine: IvoireDataEngine) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    by_language: dict[str, dict[str, Any]] = {}

    for spec in programming_specs(engine):
        stats_path = source_paths(engine.settings, spec)["raw"] / "official_docs_sync_stats.json"
        stats = load_json(stats_path, {}) if stats_path.exists() else {}
        if not isinstance(stats, dict):
            stats = {}

        language = str(spec.options.get("programming_language") or "General")
        failed = int(stats.get("failed") or 0)
        backlog = int(stats.get("backlog_count") or 0)
        complete = bool(
            stats
            and stats.get("discovery_complete")
            and not stats.get("discovery_truncated")
            and failed == 0
            and backlog == 0
        )
        row = {
            "source_id": spec.source_id,
            "title": spec.title,
            "programming_language": language,
            "framework": spec.options.get("framework"),
            "runtime": spec.options.get("runtime"),
            "tool": spec.options.get("tool"),
            "source_url": spec.source_url,
            "public_docs_url": spec.options.get("public_docs_url"),
            "source_strategy": stats.get("source_strategy") or spec.options.get("source_strategy") or "OFFICIAL_WEB",
            "doc_version": spec.options.get("doc_version"),
            "version_policy": spec.options.get("version_policy"),
            "git_repository": stats.get("git_repository"),
            "git_ref": stats.get("git_ref"),
            "git_commit": stats.get("git_commit"),
            "discovery_methods": stats.get("discovery_methods", []),
            "discovery_complete": bool(stats.get("discovery_complete", False)),
            "discovery_truncated": bool(stats.get("discovery_truncated", False)),
            "discovered_pages": int(stats.get("discovered_pages") or 0),
            "selected_pages": int(stats.get("selected_pages") or 0),
            "downloaded": int(stats.get("downloaded") or 0),
            "downloaded_bytes": int(stats.get("downloaded_bytes") or 0),
            "replayed_from_local_cache": int(stats.get("replayed_from_local_cache") or 0),
            "unchanged_git": int(stats.get("unchanged_git") or 0),
            "unchanged_lastmod": int(stats.get("unchanged_lastmod") or 0),
            "unchanged_http304": int(stats.get("unchanged_http304") or 0),
            "unchanged_sha256": int(stats.get("unchanged_sha256") or 0),
            "content_unchanged": int(stats.get("content_unchanged") or 0),
            "new_documents": int(stats.get("new_documents") or 0),
            "modified_documents": int(stats.get("modified_documents") or 0),
            "removed_upstream": int(stats.get("removed_upstream") or 0),
            "body_requests_avoided": int(stats.get("body_requests_avoided") or 0),
            "incremental_efficiency": float(stats.get("incremental_efficiency") or 0.0),
            "chunks_created": int(stats.get("chunks_created") or 0),
            "chunks_reused": int(stats.get("chunks_reused") or 0),
            "failed": failed,
            "backlog_count": backlog,
            "business_chunks": int(stats.get("business_chunks") or 0),
            "complete": complete,
        }
        rows.append(row)

        group = by_language.setdefault(language, {
            "sources": 0,
            "complete_sources": 0,
            "selected_pages": 0,
            "business_chunks": 0,
            "downloaded": 0,
            "downloaded_bytes": 0,
            "body_requests_avoided": 0,
            "backlog_count": 0,
            "failed": 0,
            "frameworks": [],
            "runtimes": [],
            "tools": [],
        })
        group["sources"] += 1
        group["complete_sources"] += int(complete)
        group["selected_pages"] += row["selected_pages"]
        group["business_chunks"] += row["business_chunks"]
        group["downloaded"] += row["downloaded"]
        group["downloaded_bytes"] += row["downloaded_bytes"]
        group["body_requests_avoided"] += row["body_requests_avoided"]
        group["backlog_count"] += backlog
        group["failed"] += failed
        for key, bucket in (("framework", "frameworks"), ("runtime", "runtimes"), ("tool", "tools")):
            value = row.get(key)
            if value and value not in group[bucket]:
                group[bucket].append(value)

    for group in by_language.values():
        group["frameworks"].sort()
        group["runtimes"].sort()
        group["tools"].sort()
        total = int(group["selected_pages"] or 0)
        group["incremental_efficiency"] = round((int(group["body_requests_avoided"] or 0) / total) * 100, 2) if total else 0.0
        group["complete"] = (
            group["sources"] == group["complete_sources"]
            and group["backlog_count"] == 0
            and group["failed"] == 0
        )

    complete_sources = sum(1 for row in rows if row["complete"])
    selected_pages = sum(row["selected_pages"] for row in rows)
    body_requests_avoided = sum(row["body_requests_avoided"] for row in rows)
    return {
        "scope": "GLOBAL_PROGRAMMING_DOCUMENTATION",
        "summary": {
            "registered_sources": len(rows),
            "complete_sources": complete_sources,
            "incomplete_sources": len(rows) - complete_sources,
            "languages": len(by_language),
            "selected_pages": selected_pages,
            "business_chunks": sum(row["business_chunks"] for row in rows),
            "downloaded": sum(row["downloaded"] for row in rows),
            "downloaded_bytes": sum(row["downloaded_bytes"] for row in rows),
            "body_requests_avoided": body_requests_avoided,
            "incremental_efficiency": round((body_requests_avoided / selected_pages) * 100, 2) if selected_pages else 0.0,
            "backlog_count": sum(row["backlog_count"] for row in rows),
            "failed": sum(row["failed"] for row in rows),
            "complete": bool(rows and complete_sources == len(rows)),
        },
        "by_language": dict(sorted(by_language.items())),
        "rows": sorted(rows, key=lambda row: (row["programming_language"].casefold(), row["source_id"])),
    }


def sync_programming_docs(
    engine: IvoireDataEngine,
    *,
    language: str | None = None,
    force: bool = False,
    due_only: bool = False,
):
    wanted = language.casefold() if language else None
    results = []
    for spec in programming_specs(engine):
        current = str(spec.options.get("programming_language") or "General")
        if wanted and current.casefold() != wanted:
            continue
        if due_only and not engine.freshness.due(spec):
            continue
        results.append(engine.sync(spec.source_id, force=force))
    return results


def write_programming_docs_report(engine: IvoireDataEngine) -> dict[str, Any]:
    report = programming_docs_audit(engine)
    root = engine.settings.data_dir / "reports" / "programming-docs"
    root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(root / "programming-docs-report.json", report)
    atomic_write_json(root / "languages.json", report["by_language"])
    return {"report": report, "output_dir": str(root)}
