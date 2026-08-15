from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from ivoiredata.artifact_ledger import ArtifactLedger
from ivoiredata.artifact_repair import execute_direct_phase, proposed_action
from ivoiredata.models import SourceSpec
from ivoiredata.upstream_state import UpstreamState


class Registry:
    def __init__(self, spec):
        self.spec = spec

    def get(self, source_id):
        assert source_id == self.spec.source_id
        return self.spec


class FakeResponse:
    def __init__(self, raw: bytes, *, status_code: int = 200):
        self.raw = raw
        self.status_code = status_code
        self.headers = {"content-type": "application/pdf", "etag": '"repair-v1"'}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024 * 1024):
        for start in range(0, len(self.raw), max(1, chunk_size)):
            yield self.raw[start:start + chunk_size]


def _engine(tmp_path: Path):
    spec = SourceSpec(
        source_id="civ_sgg_official_texts",
        title="SGG",
        domain="law_justice",
        provider="SGG",
        source_url="https://web.sgg.gouv.ci/accueil",
        rights_tier="C_PUBLIC_LOCAL_INGEST",
        access_tier="OPEN_PUBLIC",
        priority="P0",
        connector="public_web",
        options={"max_bytes": 2_000_000, "verify_ssl": True},
    )
    upstreams = UpstreamState(tmp_path / "upstreams.json")
    settings = SimpleNamespace(data_dir=tmp_path / "data", user_agent="IvoireData-test")
    return SimpleNamespace(settings=settings, registry=Registry(spec), upstreams=upstreams), spec


def _missing_row(engine, ledger, *, url: str, raw: bytes):
    digest = hashlib.sha256(raw).hexdigest()
    artifact_id = f"url:{url}"
    row = engine.upstreams.mark_downloaded(
        "civ_sgg_official_texts",
        artifact_id,
        url=url,
        signature=digest,
        sha256=digest,
        size_bytes=len(raw),
        method="HISTORICAL",
        local_path="/definitely/missing/file.pdf",
    )
    ledger.ingest_upstream_row(row)
    return artifact_id, digest


def test_direct_repair_restores_undiscoverable_same_sha(tmp_path, monkeypatch):
    raw = b"historical-pdf-bytes"
    url = "https://web.sgg.gouv.ci/uploads/publications/1340207289Circulaire_du_PM.pdf"
    engine, _ = _engine(tmp_path)
    ledger = ArtifactLedger(tmp_path / "ledger.sqlite3")
    try:
        artifact_id, digest = _missing_row(engine, ledger, url=url, raw=raw)
        assert ledger.get("civ_sgg_official_texts", artifact_id)["status"] == "LOCAL_MISSING"
        monkeypatch.setattr("requests.get", lambda *args, **kwargs: FakeResponse(raw))

        result = execute_direct_phase(
            engine,
            ledger,
            [{"source_id": "civ_sgg_official_texts", "artifact_id": artifact_id, "status": "LOCAL_MISSING"}],
        )
        assert result[0]["status"] == "REPAIRED"
        assert result[0]["sha256"] == digest
        repaired = ledger.get("civ_sgg_official_texts", artifact_id)
        assert repaired["status"] == "FETCHED"
        assert Path(repaired["local_path"]).read_bytes() == raw
        assert engine.upstreams.get("civ_sgg_official_texts", artifact_id)["method"] == "ARTIFACT_DIRECT_REPAIR"
        verified = ledger.verify(source_id="civ_sgg_official_texts")
        assert verified["verified"] == 1
        assert verified["corrupted"] == 0
    finally:
        ledger.close()


def test_direct_repair_rejects_changed_upstream_bytes(tmp_path, monkeypatch):
    old = b"old-pdf"
    changed = b"changed-pdf"
    url = "https://web.sgg.gouv.ci/uploads/publications/document.pdf"
    engine, _ = _engine(tmp_path)
    ledger = ArtifactLedger(tmp_path / "ledger.sqlite3")
    try:
        artifact_id, _ = _missing_row(engine, ledger, url=url, raw=old)
        monkeypatch.setattr("requests.get", lambda *args, **kwargs: FakeResponse(changed))
        result = execute_direct_phase(
            engine,
            ledger,
            [{"source_id": "civ_sgg_official_texts", "artifact_id": artifact_id, "status": "LOCAL_MISSING"}],
        )
        assert result[0]["status"] == "FAILED_DIRECT_REPAIR"
        assert "sha256 mismatch" in result[0]["error"]
        assert ledger.get("civ_sgg_official_texts", artifact_id)["status"] == "LOCAL_MISSING"
        assert not list((tmp_path / "data").rglob("*.part"))
    finally:
        ledger.close()


def test_upload_directory_without_trailing_slash_is_tombstoned(tmp_path):
    url = "https://web.sgg.gouv.ci/uploads/publications"
    engine, _ = _engine(tmp_path)
    ledger = ArtifactLedger(tmp_path / "ledger.sqlite3")
    try:
        artifact_id = f"url:{url}"
        row = engine.upstreams.mark_error(
            "civ_sgg_official_texts",
            artifact_id,
            url=url,
            error="403",
            status_code=403,
            method="HTTP_DOCUMENT",
        )
        ledger.ingest_upstream_row(row)
        full = ledger.get("civ_sgg_official_texts", artifact_id)
        assert proposed_action(engine, full) == "TOMBSTONE_INVALID_LEGACY_URL"
        result = execute_direct_phase(
            engine,
            ledger,
            [{"source_id": "civ_sgg_official_texts", "artifact_id": artifact_id, "status": "FAILED"}],
        )
        assert result[0]["status"] == "REMOVED"
        assert ledger.get("civ_sgg_official_texts", artifact_id)["status"] == "REMOVED"
    finally:
        ledger.close()
