from __future__ import annotations

import hashlib

from ivoiredata.streaming_snapshot import finalize_temp_snapshot, new_temp_path


def test_snapshot_filename_stays_below_filesystem_component_limit(tmp_path):
    raw = b"x,y\n1,2\n"
    temp = new_temp_path(tmp_path, prefix="dataset")
    temp.write_bytes(raw)
    very_long_id = "ratios-elevessalle-" + ("enseignement-primaire-" * 20) + "2012-2013"
    snapshot = finalize_temp_snapshot(
        tmp_path,
        temp_path=temp,
        source_id="civ_datagouv_catalog",
        url=f"https://data.gouv.ci/data-fair/api/v1/datasets/{very_long_id}/full",
        content_type="text/csv",
        name=f"{very_long_id}.csv",
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
    )
    path = snapshot["local_path"]
    assert len(str(path).rsplit("/", 1)[-1].encode("utf-8")) <= 240
