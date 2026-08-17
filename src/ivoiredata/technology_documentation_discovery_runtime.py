from __future__ import annotations

from typing import Any

from . import technology_documentation_discovery as _discovery
from .technology_discovery import normalize_registry
from .technology_documentation_discovery import (
    ActiveDocumentationDiscovery as _BaseDiscovery,
    Candidate,
)


# AppDoc artifact pages are third-party package aggregation/landing pages. They may be
# retained as discovery evidence, but they must never be promoted as canonical project
# documentation. Keep the low-value candidate filter aligned with the qualification
# policy used by _demote_weak_registry_target().
_discovery._LOW_VALUE_HOSTS.update({"appdoc.app", "www.appdoc.app"})


class ActiveDocumentationDiscovery(_BaseDiscovery):
    """Runtime-hardened Active Docs Discovery.

    Stage-3 target mutations create a new ``last_resolved_at`` generation. The base
    discovery result must checkpoint that *new* generation, otherwise the next command
    would immediately rediscover the same package because the target appears newer
    than the just-written discovery row.

    The same refresh also reloads evidence/status fields after a legacy registry target
    is demoted. This matters when discovery finds the replacement in the same command:
    promotion must append to the freshly persisted demotion evidence rather than
    overwriting it with the stale pre-demotion row snapshot.

    Persisted weak targets are selected explicitly, including AppDoc artifact pages
    created by older authority/cross-check policy. This makes the migration repairable
    in-place without forcing a Maven requalification or authority refresh.
    """

    def _refresh_target_row(self, row: dict[str, Any]) -> None:
        current = self.db.execute(
            """
            SELECT last_resolved_at,evidence_json,target_status,target_kind,
                   target_confidence,target_url,source_strategy
            FROM documentation_targets
            WHERE registry=? AND name=?
            """,
            (row["registry"], row["name"]),
        ).fetchone()
        if current is None:
            return
        for key in (
            "last_resolved_at",
            "evidence_json",
            "target_status",
            "target_kind",
            "target_confidence",
            "target_url",
            "source_strategy",
        ):
            if current[key] is not None:
                row[key] = current[key]

    def _eligible(self, *, limit: int, registry: str | None) -> list[dict[str, Any]]:
        if int(limit) <= 0:
            raise ValueError("active documentation discovery is intentionally bounded; --limit must be > 0")
        normalized = normalize_registry(registry) if registry else None
        now = _discovery._iso()
        weak_target_sql = """(
            d.target_url LIKE 'https://central.sonatype.com/%'
            OR d.target_url LIKE 'http://central.sonatype.com/%'
            OR d.target_url LIKE 'https://search.maven.org/%'
            OR d.target_url LIKE 'http://search.maven.org/%'
            OR d.target_url LIKE 'https://appdoc.app/%'
            OR d.target_url LIKE 'http://appdoc.app/%'
            OR d.target_url LIKE 'https://www.appdoc.app/%'
            OR d.target_url LIKE 'http://www.appdoc.app/%'
        )"""
        where = [
            "a.authority_status='AUTHORITY_VERIFIED'",
            "(d.target_status IN ('DOCS_DISCOVERY_REQUIRED','DOCS_TARGET_MISSING') "
            f"OR {weak_target_sql})",
            "(x.registry IS NULL OR d.last_resolved_at>x.target_resolved_at "
            "OR (x.discovery_status IN ('RETRY','NO_MATCH') AND x.attempts<? "
            "AND (x.next_retry_at IS NULL OR x.next_retry_at<=?)))",
        ]
        params: list[Any] = [self.max_attempts, now]
        if normalized:
            where.append("d.registry=?")
            params.append(normalized)
        params.append(int(limit))
        rows = self.db.execute(
            """
            SELECT d.*,a.last_checked_at AS authority_last_checked_at,
                   a.documentation_url AS authority_documentation_url,
                   a.official_website AS authority_official_website,
                   a.canonical_repository AS authority_repository,
                   a.attempts AS authority_attempts_live
            FROM documentation_targets AS d
            JOIN authority_results AS a ON a.registry=d.registry AND a.name=d.name
            LEFT JOIN documentation_discovery_results AS x
              ON x.registry=d.registry AND x.name=d.name
            WHERE """
            + " AND ".join(where)
            + " ORDER BY d.programming_language ASC,d.registry ASC,d.name ASC LIMIT ?",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def _promote_target(self, row: dict[str, Any], candidate: Candidate) -> None:
        super()._promote_target(row, candidate)
        self._refresh_target_row(row)

    def _demote_weak_registry_target(self, row: dict[str, Any]) -> None:
        super()._demote_weak_registry_target(row)
        self._refresh_target_row(row)

    def _validate_selected(self, candidate: Candidate) -> Candidate:
        # A GitHub docs directory comes directly from the contents API of the already
        # verified canonical repository. Re-fetching its HTML page adds no authority
        # evidence and can be much larger than a normal discovery page.
        if candidate.kind == "CANONICAL_REPOSITORY_DOCS_DIRECTORY":
            return candidate
        return super()._validate_selected(candidate)

    def audit(self, *, top: int = 50) -> dict[str, Any]:
        payload = super().audit(top=top)
        weak_rows = self.db.execute(
            """
            SELECT COUNT(*) AS n FROM documentation_targets
            WHERE target_status='READY_FOR_DOCS_CONNECTOR'
              AND (
                   target_url LIKE 'https://central.sonatype.com/%'
                OR target_url LIKE 'http://central.sonatype.com/%'
                OR target_url LIKE 'https://search.maven.org/%'
                OR target_url LIKE 'http://search.maven.org/%'
                OR target_url LIKE 'https://appdoc.app/%'
                OR target_url LIKE 'http://appdoc.app/%'
                OR target_url LIKE 'https://www.appdoc.app/%'
                OR target_url LIKE 'http://www.appdoc.app/%'
              )
            """
        ).fetchone()
        payload["weak_registry_targets_still_ready"] = int(
            weak_rows["n"] if weak_rows else 0
        )
        return payload
