from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ivoiredata.connectors.faostat import _matches_country
from ivoiredata.connectors.ilostat import _csv_rows
from ivoiredata.connectors.uis import _rows as uis_rows
from ivoiredata.delivery import compute_delivery_status, write_source_manifest
from ivoiredata.models import SourceSpec
from ivoiredata.settings import Settings


def _spec(**kwargs) -> SourceSpec:
    data = {
        "source_id": "test_source",
        "title": "Test Source",
        "domain": "test",
        "provider": "Test",
        "source_url": "https://example.test/data",
        "rights_tier": "A_REDISTRIBUTABLE",
        "access_tier": "OPEN",
        "priority": "P1",
        "connector": "http_file",
        "auto_sync": True,
        "refresh_hours": 24,
        "options": {},
    }
    data.update(kwargs)
    return SourceSpec(**data)


def _inventory(*, rows=0, table_files=0, raw_files=0, document_files=0):
    return {
        "tables": {"files": table_files, "bytes": 123 if table_files else 0, "rows": rows},
        "raw": {"files": raw_files, "bytes": 456 if raw_files else 0},
        "documents": {"files": document_files, "bytes": 789 if document_files else 0},
    }


def test_delivery_status_classifies_real_outputs():
    assert compute_delivery_status(_spec(), sync_status="success", inventory=_inventory(rows=10, table_files=1))[0] == "FULL_STRUCTURED"
    assert compute_delivery_status(_spec(), sync_status="success", inventory=_inventory(document_files=2))[0] == "DOCUMENTS_ONLY"
    assert compute_delivery_status(_spec(), sync_status="success", inventory=_inventory(raw_files=1))[0] == "SNAPSHOT_ONLY"
    status, warnings = compute_delivery_status(_spec(), sync_status="success", inventory=_inventory())
    assert status == "EMPTY"
    assert "EMPTY_AFTER_SUCCESS" in warnings


def test_delivery_status_marks_metadata_only_and_degraded_tls():
    spec = _spec(connector="public_web", options={"metadata_only": True, "verify_ssl": False})
    status, warnings = compute_delivery_status(spec, sync_status="success", inventory=_inventory(document_files=1))
    assert status == "METADATA_ONLY"
    assert "METADATA_ONLY_SOURCE" in warnings
    assert "TLS_VERIFICATION_DISABLED" in warnings


def test_error_with_existing_data_is_stale_not_empty(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "lake", state_dir=tmp_path / "state")
    spec = _spec()
    tables = settings.data_dir / "domains" / "test" / "test_source" / "tables" / "records"
    tables.mkdir(parents=True)
    pq.write_table(pa.table({"x": [1, 2, 3]}), tables / "part.parquet")
    manifest = write_source_manifest(
        settings,
        spec,
        status="error",
        connector=spec.connector,
        started_at="2026-08-09T20:00:00Z",
        finished_at="2026-08-09T20:01:00Z",
        details="upstream 500",
        freshness_state={"last_attempt": "2026-08-09T20:01:00Z", "last_success": "2026-08-08T20:00:00Z", "last_status": "error"},
        due=True,
    )
    assert manifest["delivery_status"] == "FULL_STRUCTURED"
    assert manifest["delivery"]["rows"] == 3
    assert manifest["freshness_status"] == "STALE"
    assert "SYNC_ERROR_WITH_STALE_DATA" in manifest["warnings"]


def test_ilostat_csv_preserves_all_observation_statuses():
    content = (
        "ref_area,indicator,time,obs_value,obs_status\n"
        "CIV,IND_A,2023,1.0,A\n"
        "CIV,IND_A,2024,1.1,R\n"
    ).encode()
    rows = _csv_rows(content)
    assert len(rows) == 2
    assert {row["obs_status"] for row in rows} == {"A", "R"}


def test_faostat_country_matching_accepts_cote_divoire_aliases():
    aliases = {"Côte d'Ivoire", "Cote d'Ivoire", "Ivory Coast"}
    assert _matches_country({"Area": "Côte d'Ivoire"}, aliases)
    assert _matches_country({"Country Name": "Ivory Coast"}, aliases)
    assert not _matches_country({"Area": "France"}, aliases)


def test_uis_payload_rows_handles_common_shapes():
    assert uis_rows([{"x": 1}]) == [{"x": 1}]
    assert uis_rows({"data": [{"x": 2}]}) == [{"x": 2}]
    assert uis_rows({"A": {"x": 3}, "B": {"x": 4}}) == [{"x": 3}, {"x": 4}]
