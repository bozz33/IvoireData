from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from .technology_discovery import normalize_registry
from .technology_qualification import TechnologyQualificationEngine as _BaseQualificationEngine


_MAVEN_REGISTRY = "repo1.maven.org"
_REGISTRY_LANDING_HOSTS = {"central.sonatype.com", "search.maven.org", "repo1.maven.org", "repo.maven.apache.org"}


def _is_registry_landing_url(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        host = (urlparse(text).hostname or "").casefold()
    except ValueError:
        return False
    return host in _REGISTRY_LANDING_HOSTS


def _sanitize_native_for_policy(registry: str, native: dict[str, Any]) -> dict[str, Any]:
    """Remove registry landing pages that are not project documentation evidence.

    Older Maven qualification rows (created before this policy) stored Central's
    artifact page as both ``documentation_url`` and sometimes ``official_website``.
    Recalibration must correct those rows locally without recontacting Maven.
    """
    cleaned = dict(native)
    if normalize_registry(registry) == _MAVEN_REGISTRY:
        if _is_registry_landing_url(cleaned.get("documentation_url")):
            cleaned["documentation_url"] = None
        if _is_registry_landing_url(cleaned.get("official_website")):
            cleaned["official_website"] = None
    return cleaned


class TechnologyQualificationEngine(_BaseQualificationEngine):
    """Qualification v2: separate authority readiness from popularity.

    Maven Central does not expose native download/dependent popularity telemetry, so
    its importance score is structurally low. That must affect ranking, not whether a
    package with a stable release and explicit SCM can be independently verified.
    """

    def _decision(self, candidate: dict[str, Any], native: dict[str, Any]) -> dict[str, Any]:
        registry = normalize_registry(str(candidate["registry"]))
        cleaned = _sanitize_native_for_policy(registry, native)
        result = super()._decision(candidate, cleaned)

        if registry == _MAVEN_REGISTRY:
            stable = bool(result.get("latest_stable_version"))
            repository = bool(result.get("canonical_repository"))
            native_score = int(result.get("native_officiality_score") or 0)
            # Maven native evidence after removing Central landing pages normally
            # scores 60 with stable+SCM and 65 with stable+SCM+project website. This
            # is enough to enter the *independent authority verification* stage, but
            # it does not inflate importance/tier or candidate priority.
            if stable and repository and native_score >= 60:
                result["qualification_status"] = "READY_FOR_AUTHORITY"
                result["next_action"] = "AUTHORITY_RESOLUTION"
                evidence = list(result.get("evidence") or [])
                marker = "MAVEN_METADATA_READY_FOR_AUTHORITY"
                if marker not in evidence:
                    evidence.append(marker)
                result["evidence"] = evidence

        return result

    def recalibrate(
        self,
        *,
        limit: int = 1000,
        registry: str | None = None,
    ) -> dict[str, Any]:
        """Recompute stored qualification decisions using local metadata only.

        No native registry request is made. This is specifically safe for server
        migrations: already-qualified Maven packages can adopt the corrected policy
        without re-downloading maven-metadata.xml or POM files.
        """
        if int(limit) <= 0:
            raise ValueError("qualification recalibration is intentionally bounded; --limit must be > 0")
        normalized = normalize_registry(registry) if registry else None
        where = ["q.qualification_status NOT IN ('NOT_FOUND','DEFERRED_UNSUPPORTED')"]
        params: list[Any] = []
        if normalized:
            where.append("q.registry=?")
            params.append(normalized)
        params.append(int(limit))
        rows = self.db.execute(
            """
            SELECT q.*,c.priority AS live_priority
            FROM qualification_results AS q
            JOIN candidates AS c ON c.registry=q.registry AND c.name=q.name
            WHERE """
            + " AND ".join(where)
            + " ORDER BY q.last_checked_at ASC,q.registry ASC,q.name ASC LIMIT ?",
            params,
        ).fetchall()

        by_before: dict[str, int] = {}
        by_after: dict[str, int] = {}
        changed = 0
        outcomes: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            before = str(row.get("qualification_status") or "UNKNOWN")
            try:
                native = json.loads(str(row.get("metadata_json") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                native = {}
            if not isinstance(native, dict) or not native:
                continue
            candidate = {
                "registry": row["registry"],
                "name": row["name"],
                "priority": int(row.get("live_priority") or row.get("candidate_priority") or 0),
            }
            result = self._decision(candidate, native)
            after = str(result.get("qualification_status") or "UNKNOWN")
            # Preserve the original native payload for provenance; _decision may
            # sanitize policy-only fields in its working copy.
            result["metadata"] = native
            with self.db:
                self._upsert_result_no_commit(result)
            by_before[before] = by_before.get(before, 0) + 1
            by_after[after] = by_after.get(after, 0) + 1
            if before != after:
                changed += 1
            outcomes.append(
                {
                    "registry": row["registry"],
                    "name": row["name"],
                    "before": before,
                    "after": after,
                    "score": int(result.get("qualification_score") or 0),
                    "native_score": int(result.get("native_officiality_score") or 0),
                }
            )
        return {
            "engine": "qualification-v2",
            "recalibrated": len(outcomes),
            "changed_status": changed,
            "by_before": dict(sorted(by_before.items())),
            "by_after": dict(sorted(by_after.items())),
            "network_requests": 0,
            "outcomes": outcomes[:100],
        }
