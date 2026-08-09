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
        """Whether the source is safe for unattended public ingestion.

        OPEN and OPEN_PUBLIC are synchronizable. MIXED, controlled/research
        access and any D_* rights tier stay manual by design.
        """
        access = self.access_tier.upper()
        rights = self.rights_tier.upper()
        return access in {"OPEN", "OPEN_PUBLIC"} and not rights.startswith("D_")


@dataclass
class SyncResult:
    source_id: str
    status: str
    started_at: str
    finished_at: str
    connector: str
    details: str = ""
