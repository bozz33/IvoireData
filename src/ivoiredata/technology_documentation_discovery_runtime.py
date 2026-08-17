from __future__ import annotations

from typing import Any

from .technology_documentation_discovery import (
    ActiveDocumentationDiscovery as _BaseDiscovery,
    Candidate,
)


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
