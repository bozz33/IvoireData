from __future__ import annotations

import os
from pathlib import Path

import pyarrow.parquet as pq

from ivoiredata.dlt_tables import replace_table_item


def _rows(root: Path, table: str) -> list[dict]:
    files = list((root / "dataset" / table).glob("*.parquet"))
    rows: list[dict] = []
    for file in files:
        rows.extend(pq.read_table(file).to_pylist())
    return rows


def test_dynamic_table_variant_replace_does_not_truncate_unemitted_table(tmp_path: Path, monkeypatch):
    import dlt

    bucket = tmp_path / "bucket"
    monkeypatch.setenv("DESTINATION__FILESYSTEM__BUCKET_URL", bucket.resolve().as_uri())

    @dlt.resource(name="multi")
    def first():
        yield replace_table_item(dlt, {"id": 1, "value": "a-v1"}, "table_a")
        yield replace_table_item(dlt, {"id": 1, "value": "b-v1"}, "table_b")

    pipeline = dlt.pipeline(
        pipeline_name="variant_safety",
        destination="filesystem",
        dataset_name="dataset",
        pipelines_dir=str(tmp_path / "pipelines"),
    )
    pipeline.run(first(), loader_file_format="parquet")
    assert [r["value"] for r in _rows(bucket, "table_a")] == ["a-v1"]
    assert [r["value"] for r in _rows(bucket, "table_b")] == ["b-v1"]

    @dlt.resource(name="multi")
    def second():
        # table_a is intentionally absent: an incremental connector concluded it is unchanged.
        yield replace_table_item(dlt, {"id": 1, "value": "b-v2"}, "table_b")

    pipeline.run(second(), loader_file_format="parquet")
    assert [r["value"] for r in _rows(bucket, "table_a")] == ["a-v1"]
    assert [r["value"] for r in _rows(bucket, "table_b")] == ["b-v2"]
