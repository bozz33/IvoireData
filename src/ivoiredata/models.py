from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    title: str
    domain: str
    provider: str
    source_url: str
    rights_tier: str
    access_tier: str
    priority: str
    connector: str = "auto"
    refresh_hours: int = 168
    auto_sync: bool = False
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def public(self) -> bool:
        """Whether unattended ingestion is permitted by IvoireData policy.

        OPEN/OPEN_PUBLIC payloads are ingestible. MIXED or controlled sources
        remain blocked unless runtime configuration explicitly sets
        ``metadata_only=true``; in that case only their public catalog/docs are
        synchronized. D_* sources remain excluded from unattended ingestion.
        """
        access = self.access_tier.upper()
        rights = self.rights_tier.upper()
        if rights.startswith("D_"):
            return False
        if access in {"OPEN", "OPEN_PUBLIC"}:
            return True
        return bool(self.options.get("metadata_only", False))


@dataclass
class SyncResult:
    source_id: str
    status: str
    started_at: str
    finished_at: str
    connector: str
    details: str = ""
