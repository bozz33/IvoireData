from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, TYPE_CHECKING, Iterable

from .locks import file_lock
from .state_io import atomic_write_json, load_json

if TYPE_CHECKING:
    from .registry import SourceRegistry
    from .settings import Settings


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    data = load_json(path, {})
    return data if isinstance(data, dict) else {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def load_runtime_config(
    base_path: Path | None,
    overrides_path: Path | None = None,
    overlay_paths: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Merge packaged defaults, packaged CI Gold overlays, then mutable local overrides."""
    merged = _read_json(base_path)
    for path in overlay_paths or []:
        merged = _deep_merge(merged, _read_json(path))
    return _deep_merge(merged, _read_json(overrides_path))


class RuntimeControl:
    """Persistent runtime settings stored under `.ivoiredata/state`.

    Versioned config files are immutable defaults. User changes live in
    `runtime_overrides.json`, shared by API, scheduler and one-shot containers.
    Read-modify-write mutations are protected by a shared file lock so two API
    requests cannot silently overwrite each other.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.runtime_overrides_path
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @property
    def overlay_paths(self) -> list[Path]:
        return [self.settings.ci_gold_runtime_path]

    def overrides(self) -> dict[str, Any]:
        return _read_json(self.path)

    def merged(self) -> dict[str, Any]:
        return load_runtime_config(
            self.settings.runtime_config_path,
            self.path,
            self.overlay_paths,
        )

    @property
    def automatic_enabled(self) -> bool:
        return bool(self.merged().get("updates", {}).get("automatic_enabled", True))

    @property
    def scheduler_interval_seconds(self) -> int:
        value = int(self.merged().get("updates", {}).get("scheduler_interval_seconds", 3600))
        return max(300, value)

    def _write(self, data: dict[str, Any]) -> None:
        atomic_write_json(self.path, data)

    def set_updates(
        self,
        *,
        automatic_enabled: bool | None = None,
        scheduler_interval_seconds: int | None = None,
    ) -> dict[str, Any]:
        with file_lock(self.lock_path, timeout=60):
            data = self.overrides()
            updates = data.setdefault("updates", {})
            if not isinstance(updates, dict):
                updates = {}
                data["updates"] = updates
            if automatic_enabled is not None:
                updates["automatic_enabled"] = bool(automatic_enabled)
            if scheduler_interval_seconds is not None:
                interval = int(scheduler_interval_seconds)
                if interval < 300:
                    raise ValueError("scheduler_interval_seconds must be >= 300")
                updates["scheduler_interval_seconds"] = interval
            self._write(data)
        return self.merged().get("updates", {})

    def set_source(self, source_id: str, **changes: Any) -> dict[str, Any]:
        with file_lock(self.lock_path, timeout=60):
            data = self.overrides()
            sources = data.setdefault("sources", {})
            if not isinstance(sources, dict):
                sources = {}
                data["sources"] = sources
            entry = sources.setdefault(source_id, {})
            if not isinstance(entry, dict):
                entry = {}
                sources[source_id] = entry
            for key in ("enabled", "auto_sync", "refresh_hours"):
                if key not in changes or changes[key] is None:
                    continue
                value = changes[key]
                if key == "refresh_hours":
                    value = int(value)
                    if value < 1:
                        raise ValueError("refresh_hours must be >= 1")
                elif key in {"enabled", "auto_sync"}:
                    value = bool(value)
                entry[key] = value
            self._write(data)
        return self.merged().get("sources", {}).get(source_id, {})

    def source_status(self, registry: SourceRegistry, source_id: str) -> dict[str, Any]:
        spec = registry.get(source_id)
        mode = "DISABLED" if not spec.enabled else ("AUTOMATIC" if spec.auto_sync else "MANUAL")
        return {
            "source_id": spec.source_id,
            "enabled": spec.enabled,
            "update_mode": mode,
            "refresh_hours": spec.refresh_hours,
            "public": spec.public,
        }

    def status(self, registry: SourceRegistry) -> dict[str, Any]:
        all_specs = registry.all()
        enabled = [s for s in all_specs if s.enabled]
        public_enabled = [s for s in enabled if s.public]
        return {
            "automatic_enabled": self.automatic_enabled,
            "scheduler_interval_seconds": self.scheduler_interval_seconds,
            "registry_sources": len(all_specs),
            "enabled_sources": len(enabled),
            "disabled_sources": sum(1 for s in all_specs if not s.enabled),
            "automatic_public_sources": sum(1 for s in public_enabled if s.auto_sync),
            "manual_public_sources": sum(1 for s in public_enabled if not s.auto_sync),
            "controlled_sources": sum(1 for s in enabled if not s.public),
            "overrides_path": str(self.path),
            "packaged_overlays": [str(path) for path in self.overlay_paths if path.exists()],
        }
