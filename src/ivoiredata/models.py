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
        return self.access_tier.upper() == "OPEN" and not self.rights_tier.upper().startswith("D_")

@dataclass
class SyncResult:
    source_id: str
    status: str
    started_at: str
    finished_at: str
    connector: str
    details: str = ""
