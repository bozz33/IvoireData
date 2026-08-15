from __future__ import annotations

import hashlib
from pathlib import Path

from ivoiredata.connectors.public_web import (
    _backfill_same_digest_body,
    _normalize_url,
    _retire_invalid_legacy_artifacts,
    _same_host_links,
)
from ivoiredata.upstream_state import UpstreamState


def test_same_host_links_trim_trailing_whitespace_and_skip_upload_directories():
    links = _same_host_links(
        "https://web.sgg.gouv.ci/accueil",
        [
            " /uploads/publications/ ",
            "/uploads/publications/loi-2013.pdf%20",
            " /textes/decret-2024 ",
        ],
    )
    assert "https://web.sgg.gouv.ci/uploads/publications/" not in links
    assert "https://web.sgg.gouv.ci/uploads/publications/loi-2013.pdf" in links
    assert "https://web.sgg.gouv.ci/textes/decret-2024" in links
    assert all(not value.endswith("%20") for value in links)


def test_normalize_url_removes_only_trailing_path_whitespace():
    assert _normalize_url(" https://example.ci/doc.pdf%20?q=a%20b ") == "https://example.ci/doc.pdf?q=a%20b"


def test_legacy_bad_crawl_artifacts_are_tombstoned(tmp_path):
    upstream = UpstreamState(tmp_path / "upstreams.json")
    upstream.mark_error(
        "civ_sgg_official_texts",
        "url:https://web.sgg.gouv.ci/uploads/publications/",
        url="https://web.sgg.gouv.ci/uploads/publications/",
        error="403",
        status_code=403,
        method="HTTP_DOCUMENT",
    )
    upstream.mark_error(
        "civ_sgg_official_texts",
        "url:https://web.sgg.gouv.ci/uploads/publications/loi.pdf%20",
        url="https://web.sgg.gouv.ci/uploads/publications/loi.pdf%20",
        error="404",
        status_code=404,
        method="HTTP_DOCUMENT",
    )

    assert _retire_invalid_legacy_artifacts(upstream, "civ_sgg_official_texts") == 2
    rows = {row["artifact_id"]: row for row in upstream.source_rows("civ_sgg_official_texts")}
    assert rows["url:https://web.sgg.gouv.ci/uploads/publications/"]["last_result"] == "REMOVED_UPSTREAM"
    assert rows["url:https://web.sgg.gouv.ci/uploads/publications/loi.pdf%20"]["last_result"] == "REMOVED_UPSTREAM"


def test_same_sha_without_local_raw_is_backfilled_not_marked_unchanged(tmp_path):
    source_id = "civ_sgg_official_texts"
    current = "https://web.sgg.gouv.ci/uploads/publications/loi-2013.pdf"
    artifact = f"url:{current}"
    raw = b"%PDF-1.4\nphysical test bytes\n%%EOF\n"
    digest = hashlib.sha256(raw).hexdigest()
    upstream = UpstreamState(tmp_path / "upstreams.json")

    # Historical logical state: the content SHA was committed, but no raw path exists.
    upstream.mark_downloaded(
        source_id,
        artifact,
        url=current,
        signature=digest,
        sha256=digest,
        size_bytes=len(raw),
        method="HTTP_DOCUMENT",
        rows=3,
        local_path=None,
    )
    cached = upstream.get(source_id, artifact)
    assert cached["downloaded"] is True
    assert not cached.get("local_path")

    snapshot = _backfill_same_digest_body(
        upstream,
        source_id=source_id,
        artifact=artifact,
        current=current,
        response_url=current,
        response_headers={"etag": '"v1"'},
        content_type="application/pdf",
        raw=raw,
        digest=digest,
        links=[],
        cached=cached,
        snapshot_dir=tmp_path / "raw",
    )

    path = Path(str(snapshot["local_path"]))
    assert path.is_file()
    assert path.read_bytes() == raw
    updated = upstream.get(source_id, artifact)
    assert updated["last_result"] == "DOWNLOADED"
    assert updated["method"] == "PHYSICAL_BACKFILL_SAME_SHA"
    assert updated["local_path"] == str(path)
    assert updated["physical_backfill"] is True
    assert updated["body_changed"] is False
