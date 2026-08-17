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
    """

    def _refresh_generation(self, row: dict[str, Any]) -> None:
        current = self.db.execute(
            """
            SELECT last_resolved_at
            FROM documentation_targets
            WHERE registry=? AND name=?
            """,
            (row["registry"], row["name"]),
        ).fetchone()
        if current is not None and current["last_resolved_at"]:
            row["last_resolved_at"] = str(current["last_resolved_at"])

    def _promote_target(self, row: dict[str, Any], candidate: Candidate) -> None:
        super()._promote_target(row, candidate)
        self._refresh_generation(row)

    def _demote_weak_registry_target(self, row: dict[str, Any]) -> None:
        super()._demote_weak_registry_target(row)
        self._refresh_generation(row)

    def _validate_selected(self, candidate: Candidate) -> Candidate:
        # A GitHub docs directory comes directly from the contents API of the already
        # verified canonical repository. Re-fetching its HTML page adds no authority
        # evidence and can be much larger than a normal discovery page.
        if candidate.kind == "CANONICAL_REPOSITORY_DOCS_DIRECTORY":
            return candidate
        return super()._validate_selected(candidate)
