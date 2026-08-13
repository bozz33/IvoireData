from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ivoiredata.connectors.http_file import _valid_cached_bytes
from ivoiredata.connectors.public_web import _cached_body
from ivoiredata.connectors.uis import _cached_json
from ivoiredata.connectors.world_bank_projects import _cached_projects, _canonical_projects
from ivoiredata.upstream_state import UpstreamState


def _store(state: UpstreamState, source: str, artifact: str, path: Path, raw: bytes) -> str:
    digest = hashlib.sha256(raw).hexdigest()
    state.mark_downloaded(
        source,
        artifact,
        url="https://example.test/data",
        signature=digest,
        sha256=digest,
        size_bytes=len(raw),
        etag='"v1"',
        method="TEST",
        local_path=str(path),
    )
    return digest


def test_http_file_cache_is_replayable_only_when_hash_matches(tmp_path: Path):
    raw = b"a,b\n1,2\n"
    path = tmp_path / "payload.csv"
    path.write_bytes(raw)
    state = UpstreamState(tmp_path / "upstreams.json")
    digest = _store(state, "source", "file", path, raw)
    cached = state.get("source", "file")

    replay = _valid_cached_bytes(path, cached)
    assert replay is not None and replay[1] == digest

    path.write_bytes(b"tampered")
    assert _valid_cached_bytes(path, cached) is None


def test_uis_cache_replay_validates_json_and_sha256(tmp_path: Path):
    payload = {"data": [{"geoUnitCode": "CIV", "value": 1}]}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    path = tmp_path / "uis.json"
    path.write_bytes(raw)
    state = UpstreamState(tmp_path / "upstreams.json")
    digest = _store(state, "civ_uis", "data", path, raw)

    cached, replay_path, replay_payload, replay_digest = _cached_json(state, "civ_uis", "data")
    assert cached["signature"] == digest
    assert replay_path == path
    assert replay_payload == payload
    assert replay_digest == digest

    path.write_text("not-json", encoding="utf-8")
    _, replay_path, replay_payload, replay_digest = _cached_json(state, "civ_uis", "data")
    assert replay_path is replay_payload is replay_digest is None


def test_world_bank_projects_aggregate_cache_is_content_addressed(tmp_path: Path):
    projects = [{"id": "P1", "name": "One"}, {"id": "P2", "name": "Two"}]
    raw = _canonical_projects(projects)
    path = tmp_path / "projects.json"
    path.write_bytes(raw)
    state = UpstreamState(tmp_path / "upstreams.json")
    digest = _store(state, "civ_worldbank_projects", "projects:CI", path, raw)

    cached, replay_path, replay_projects, replay_digest = _cached_projects(
        state, "civ_worldbank_projects", "projects:CI"
    )
    assert cached["signature"] == digest
    assert replay_path == path
    assert replay_projects == projects
    assert replay_digest == digest

    path.write_bytes(b"[]")
    _, replay_path, replay_projects, replay_digest = _cached_projects(
        state, "civ_worldbank_projects", "projects:CI"
    )
    assert replay_path is replay_projects is replay_digest is None


def test_public_web_cache_body_requires_integrity_match(tmp_path: Path):
    raw = b"<html><body>official page</body></html>"
    path = tmp_path / "page.html"
    path.write_bytes(raw)
    state = UpstreamState(tmp_path / "upstreams.json")
    _store(state, "public", "url:https://example.test", path, raw)
    cached = state.get("public", "url:https://example.test")

    replay_path, replay_raw = _cached_body(state, "public", "url:https://example.test", cached)
    assert replay_path == path
    assert replay_raw == raw

    path.write_bytes(b"corrupt")
    assert _cached_body(state, "public", "url:https://example.test", cached) == (None, None)
