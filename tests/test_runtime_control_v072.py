from __future__ import annotations

import json
from pathlib import Path

from ivoiredata import scheduler
from ivoiredata.delivery import compute_delivery_status
from ivoiredata.models import SourceSpec
from ivoiredata.registry import SourceRegistry
from ivoiredata.runtime_control import RuntimeControl
from ivoiredata.settings import Settings


def _settings(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.delenv("IVOIREDATA_RUNTIME_OVERRIDES", raising=False)
    registry = tmp_path / "sources.csv"
    registry.write_text(
        "source_id,title,domain,provider,source_url,rights_tier,access_tier,priority\n"
        "demo,Demo,test,Demo,https://example.test/data,A_REDISTRIBUTABLE,OPEN,P0\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps({
            "defaults": {"refresh_hours": 168, "auto_sync": False},
            "updates": {"automatic_enabled": True, "scheduler_interval_seconds": 3600},
            "sources": {"demo": {"connector": "http_file", "auto_sync": True}},
        }),
        encoding="utf-8",
    )
    return Settings(
        data_dir=tmp_path / "lake",
        state_dir=tmp_path / "state",
        registry_path=registry,
        runtime_config_path=runtime,
    )


def test_runtime_overrides_persist_and_are_merged(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    control = RuntimeControl(settings)
    control.set_updates(automatic_enabled=False, scheduler_interval_seconds=900)
    control.set_source("demo", enabled=True, auto_sync=False, refresh_hours=72)

    fresh_control = RuntimeControl(settings)
    registry = SourceRegistry.load(
        settings.registry_path,
        settings.runtime_config_path,
        settings.runtime_overrides_path,
    )
    spec = registry.get("demo")

    assert fresh_control.automatic_enabled is False
    assert fresh_control.scheduler_interval_seconds == 900
    assert spec.enabled is True
    assert spec.auto_sync is False
    assert spec.refresh_hours == 72
    assert settings.runtime_overrides_path.exists()


def test_disabled_source_is_excluded_from_registry_list(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    RuntimeControl(settings).set_source("demo", enabled=False)
    registry = SourceRegistry.load(
        settings.registry_path,
        settings.runtime_config_path,
        settings.runtime_overrides_path,
    )
    assert registry.get("demo").enabled is False
    assert registry.list() == []
    assert len(registry.all()) == 1


def test_scheduler_global_disable_prevents_sync(monkeypatch):
    class FakeRuntime:
        automatic_enabled = False

    class FakeEngine:
        runtime = FakeRuntime()

        def sync_due(self, **kwargs):
            raise AssertionError("sync_due must not run while automatic updates are disabled")

    monkeypatch.setattr(scheduler, "IvoireDataEngine", FakeEngine)
    assert scheduler.run_once() == []


def test_scheduler_runs_automatic_sources_when_enabled(monkeypatch):
    called = []

    class FakeRuntime:
        automatic_enabled = True

    class FakeEngine:
        runtime = FakeRuntime()

        def sync_due(self, **kwargs):
            called.append(kwargs)
            return ["ok"]

    monkeypatch.setattr(scheduler, "IvoireDataEngine", FakeEngine)
    assert scheduler.run_once() == ["ok"]
    assert called == [{"auto_only": True, "public_only": True}]


def _inventory(*, rows=0, raw_files=0, document_files=0):
    return {
        "tables": {"files": 1 if rows else 0, "bytes": 10 if rows else 0, "rows": rows},
        "raw": {"files": raw_files, "bytes": 20 if raw_files else 0},
        "documents": {"files": document_files, "bytes": 30 if document_files else 0},
    }


def _spec(connector: str, *, metadata_only: bool = False) -> SourceSpec:
    return SourceSpec(
        source_id="demo",
        title="Demo",
        domain="test",
        provider="Demo",
        source_url="https://example.test",
        rights_tier="A_REDISTRIBUTABLE",
        access_tier="OPEN",
        priority="P0",
        connector=connector,
        options={"metadata_only": True} if metadata_only else {},
    )


def test_connector_aware_delivery_regression():
    assert compute_delivery_status(
        _spec("public_web"), sync_status="success", inventory=_inventory(rows=10)
    )[0] == "DOCUMENTS_ONLY"
    assert compute_delivery_status(
        _spec("osm_geofabrik"), sync_status="success", inventory=_inventory(rows=1, raw_files=1)
    )[0] == "SNAPSHOT_ONLY"
    assert compute_delivery_status(
        _spec("data_gouv_ci"), sync_status="success", inventory=_inventory(rows=10)
    )[0] == "FULL_STRUCTURED"
    assert compute_delivery_status(
        _spec("public_web", metadata_only=True), sync_status="success", inventory=_inventory(rows=2)
    )[0] == "METADATA_ONLY"
