from __future__ import annotations

from pathlib import Path

from ivoiredata.connectors.data_gouv_ci import data_gouv_ci_resource
from ivoiredata.connectors.data_gouv_ci_v3 import (
    _attachment_entries,
    _attachment_url,
    _download_metadata_attachments,
    _has_physical_cache_v3,
    data_gouv_ci_resource_v3,
)
from ivoiredata.upstream_state import UpstreamState


class FakeResponse:
    def __init__(self, url: str, body: bytes, content_type: str = "application/zip"):
        self.url = url
        self.body = body
        self.status_code = 200
        self.ok = True
        self.headers = {"content-type": content_type, "etag": '"v1"'}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=1024 * 1024):
        for start in range(0, len(self.body), max(1, chunk_size // 2)):
            yield self.body[start:start + max(1, chunk_size // 2)]


class AttachmentSession:
    def __init__(self, bodies: dict[str, bytes]):
        self.bodies = dict(bodies)
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        name = url.rsplit("/", 1)[-1]
        return FakeResponse(url, self.bodies[name])


def test_attachment_metadata_shapes_are_normalized():
    assert _attachment_entries({"attachments": [{"name": "a.zip"}]}) == [{"name": "a.zip"}]
    assert _attachment_entries({"attachments": {"files": [{"name": "b.zip"}]}}) == [{"name": "b.zip"}]
    assert _attachment_entries({"attachments": {"c.zip": {"size": 3}}}) == [{"size": 3, "name": "c.zip"}]


def test_attachment_url_uses_official_metadata_attachments_route():
    url = _attachment_url(
        "lignes-de-transport-abidjan",
        {"name": "LEMET-gtfs.zip"},
        detail_url="https://data.gouv.ci/data-fair/api/v1/datasets/lignes-de-transport-abidjan",
    )
    assert url == (
        "https://data.gouv.ci/data-fair/api/v1/datasets/"
        "lignes-de-transport-abidjan/metadata-attachments/LEMET-gtfs.zip"
    )


def test_metadata_attachments_are_streamed_tracked_and_reused(tmp_path):
    dsid = "lignes-de-transport-abidjan"
    source_id = "civ_datagouv_catalog"
    detail_url = f"https://data.gouv.ci/data-fair/api/v1/datasets/{dsid}"
    detail = {
        "id": dsid,
        "attachments": [
            {"name": "abidjan.zip", "size": 11},
            {"name": "LEMET-gtfs.zip", "size": 9},
        ],
    }
    bodies = {
        "abidjan.zip": b"first-gtfs-archive",
        "LEMET-gtfs.zip": b"second-gtfs-archive",
    }
    upstream = UpstreamState(tmp_path / "upstreams.json")
    raw_root = tmp_path / "raw"
    session = AttachmentSession(bodies)

    materialized, stats = _download_metadata_attachments(
        session,
        source_id=source_id,
        dsid=dsid,
        detail_url=detail_url,
        detail=detail,
        raw_root=raw_root,
        upstream=upstream,
    )
    assert materialized is not None
    assert materialized.method == "ATTACHMENTS_STREAM"
    assert stats == {"attachment_files": 2, "attachments_downloaded": 2, "attachments_reused": 0}
    assert len(session.calls) == 2
    assert materialized.path.is_file()
    rows = list(materialized.rows())
    assert {row["attachment_name"] for row in rows} == {"abidjan.zip", "LEMET-gtfs.zip"}
    assert all(Path(row["local_path"]).is_file() for row in rows)

    signature = "dataset-signature"
    upstream.mark_downloaded(
        source_id,
        f"dataset:{dsid}",
        url=detail_url,
        signature=signature,
        sha256=str(materialized.snapshot["sha256"]),
        size_bytes=int(materialized.snapshot["size_bytes"]),
        method="ATTACHMENTS_STREAM",
        rows=2,
        local_path=str(materialized.snapshot["local_path"]),
        extra={"attachment_backed": True},
    )
    assert _has_physical_cache_v3(upstream, source_id, f"dataset:{dsid}", signature)

    second_session = AttachmentSession(bodies)
    materialized2, stats2 = _download_metadata_attachments(
        second_session,
        source_id=source_id,
        dsid=dsid,
        detail_url=detail_url,
        detail=detail,
        raw_root=raw_root,
        upstream=upstream,
    )
    assert materialized2 is not None
    assert stats2 == {"attachment_files": 2, "attachments_downloaded": 0, "attachments_reused": 2}
    assert second_session.calls == []

    Path(rows[0]["local_path"]).unlink()
    assert not _has_physical_cache_v3(upstream, source_id, f"dataset:{dsid}", signature)


def test_public_facade_routes_to_v3_collector():
    assert data_gouv_ci_resource is data_gouv_ci_resource_v3
