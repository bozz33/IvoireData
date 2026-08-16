from __future__ import annotations

import gzip
import hashlib
import io
import json
import struct
from pathlib import Path

from ivoiredata.technology_harvester import TechnologyHarvestQueue
from ivoiredata.technology_maven import (
    MAVEN_BOOTSTRAP_SOURCE,
    MAVEN_CHANGES_SOURCE,
    MAVEN_FULL_CHUNK,
    MAVEN_INDEX_BASE,
    MAVEN_PROPERTIES,
    MAVEN_REGISTRY,
    MavenCentralIndexHarvester,
    MavenChunkReader,
    MavenIndexEvent,
    _decode_modified_utf,
)
import ivoiredata.technology_maven_authority  # noqa: F401
from ivoiredata.technology_registries import build_purl, native_package_metadata


def _mutf(value: str) -> bytes:
    raw = value.encode("utf-16-be", "surrogatepass")
    out = bytearray()
    for i in range(0, len(raw), 2):
        unit = (raw[i] << 8) | raw[i + 1]
        if 1 <= unit <= 0x7F:
            out.append(unit)
        elif unit <= 0x7FF:
            out.extend((0xC0 | ((unit >> 6) & 0x1F), 0x80 | (unit & 0x3F)))
        else:
            out.extend((0xE0 | ((unit >> 12) & 0x0F), 0x80 | ((unit >> 6) & 0x3F), 0x80 | (unit & 0x3F)))
    return bytes(out)


def _field(name: str, value: str) -> bytes:
    key = _mutf(name)
    val = _mutf(value)
    return b"\x00" + struct.pack(">H", len(key)) + key + struct.pack(">i", len(val)) + val


def _chunk(records, timestamp_ms=1_700_000_000_000) -> bytes:
    payload = bytearray(b"\x01" + struct.pack(">q", timestamp_ms))
    for record in records:
        payload.extend(struct.pack(">i", len(record)))
        for name, value in record:
            payload.extend(_field(name, value))
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as gz:
        gz.write(payload)
    return buffer.getvalue()


def _properties(last=10, chain="chain-a", timestamp="20260816100000.000 +0000", retained=None):
    retained = retained if retained is not None else [last]
    lines = [
        "nexus.index.id=central",
        f"nexus.index.chain-id={chain}",
        f"nexus.index.timestamp={timestamp}",
        f"nexus.index.last-incremental={last}",
    ]
    for i, number in enumerate(retained):
        lines.append(f"nexus.index.incremental-{i}={number}")
    return "\n".join(lines) + "\n"


class FakeResponse:
    def __init__(self, *, text="", content=None, status_code=200):
        self.text = text
        self.content = content if content is not None else text.encode()
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), max(1, chunk_size)):
            yield self.content[i : i + chunk_size]


class FakeSession:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.routes[url]
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"no fake responses left for {url}")
            value = value.pop(0)
        return value


def _routes(full_bytes, props=None, extra=None):
    props = props or _properties()
    sha1 = hashlib.sha1(full_bytes).hexdigest()
    property_response = props if isinstance(props, list) else FakeResponse(text=props)
    routes = {
        MAVEN_INDEX_BASE + MAVEN_PROPERTIES: property_response,
        MAVEN_INDEX_BASE + MAVEN_FULL_CHUNK + ".sha1": FakeResponse(text=sha1 + "\n"),
        MAVEN_INDEX_BASE + MAVEN_FULL_CHUNK: FakeResponse(content=full_bytes),
    }
    routes.update(extra or {})
    return routes


def test_maven_chunk_reader_matches_modified_utf_and_artifact_records(tmp_path):
    text = "com.exämple:demo:1.0.0"
    encoded = _mutf(text)
    assert _decode_modified_utf(encoded) == text
    data = _chunk([
        [("i", "central"), ("u", "com.exämple|demo|1.0.0|NA|jar"), ("m", "123")],
        [("del", "com.exämple|demo|1.0.0|NA|jar"), ("m", "124")],
    ])
    path = tmp_path / "index.gz"
    path.write_bytes(data)
    events = list(MavenChunkReader(path).events())
    assert [(event.kind, event.uinfo, event.modified) for event in events] == [
        ("ADD", "com.exämple|demo|1.0.0|NA|jar", 123),
        ("REMOVE", "com.exämple|demo|1.0.0|NA|jar", 124),
    ]


def test_maven_follower_refuses_fake_head_before_bootstrap(tmp_path):
    queue = TechnologyHarvestQueue(tmp_path / "queue.sqlite3")
    session = FakeSession({})
    try:
        harvester = MavenCentralIndexHarvester(queue=queue, user_agent="test", state_dir=tmp_path, session=session)
        result = harvester.changes(limit=10)
        assert result["bootstrap_required"] is True
        assert result["processed_artifacts"] == 0
        assert result["http_work_required"] is False
        assert session.calls == []
        assert queue.cursor(MAVEN_CHANGES_SOURCE) == {}
    finally:
        queue.close()


def test_maven_bootstrap_is_pinned_bounded_and_zero_work_after_complete(tmp_path):
    full = _chunk([
        [("u", "org.example|alpha|1.0.0|NA|jar"), ("m", "10")],
        [("u", "org.example|beta|2.0.0|NA|jar"), ("m", "11")],
    ])
    session = FakeSession(_routes(full))
    queue = TechnologyHarvestQueue(tmp_path / "queue.sqlite3")
    try:
        harvester = MavenCentralIndexHarvester(queue=queue, user_agent="test", state_dir=tmp_path, session=session)
        first = harvester.bootstrap(limit=1, reset=True)
        assert first["complete"] is False
        assert first["processed_artifacts"] == 1
        assert first["changes_cursor"] is None
        snapshot = first["snapshot"]
        calls_after_first = len(session.calls)

        second = harvester.bootstrap(limit=1)
        assert second["complete"] is True
        assert second["snapshot"] == snapshot
        assert second["changes_cursor"] == 10
        assert second["registry_candidates"] == 2
        assert second["version_states"] == 2
        assert len(session.calls) == calls_after_first

        before = len(session.calls)
        rerun = harvester.bootstrap(limit=500)
        assert rerun["complete"] is True
        assert rerun["processed_artifacts"] == 0
        assert rerun["http_work_required"] is False
        assert len(session.calls) == before
    finally:
        queue.close()


def test_maven_classifier_deletes_are_idempotent_and_package_safe(tmp_path):
    queue = TechnologyHarvestQueue(tmp_path / "queue.sqlite3")
    try:
        harvester = MavenCentralIndexHarvester(queue=queue, user_agent="test", state_dir=tmp_path, session=FakeSession({}))
        cursor = {"snapshot": {"timestamp": "T"}}
        adds = [
            MavenIndexEvent(1, "ADD", "g|a|1.0|NA|jar", 1),
            MavenIndexEvent(2, "ADD", "g|a|1.0|sources|jar", 2),
        ]
        harvester._apply_events(events=adds, source=MAVEN_BOOTSTRAP_SOURCE, cursor_payload=cursor, requeue_changed=False)
        row = queue.db.execute("SELECT status FROM candidates WHERE registry=? AND name='g:a'", (MAVEN_REGISTRY,)).fetchone()
        assert row["status"] == "PENDING"

        one = MavenIndexEvent(3, "REMOVE", "g|a|1.0|sources|jar", 3)
        stats = harvester._apply_events(events=[one], source=MAVEN_CHANGES_SOURCE, cursor_payload={"inflight": {"target_timestamp": "T2"}}, requeue_changed=True)
        assert stats.deleted_packages == 0
        version = queue.db.execute("SELECT live_artifacts FROM maven_version_state").fetchone()
        assert version["live_artifacts"] == 1

        two = MavenIndexEvent(4, "REMOVE", "g|a|1.0|NA|jar", 4)
        stats = harvester._apply_events(events=[two], source=MAVEN_CHANGES_SOURCE, cursor_payload={"inflight": {"target_timestamp": "T2"}}, requeue_changed=True)
        assert stats.deleted_packages == 1
        row = queue.db.execute("SELECT status FROM candidates WHERE registry=? AND name='g:a'", (MAVEN_REGISTRY,)).fetchone()
        assert row["status"] == "DELETED"

        replay = harvester._apply_events(events=[two], source=MAVEN_CHANGES_SOURCE, cursor_payload={"inflight": {"target_timestamp": "T2"}}, requeue_changed=True)
        assert replay.deleted_packages == 0
        assert replay.replayed_artifacts == 1
        version = queue.db.execute("SELECT live_artifacts FROM maven_version_state").fetchone()
        assert version["live_artifacts"] == 0
    finally:
        queue.close()


def test_maven_incremental_chain_is_pinned_and_advances_only_when_complete(tmp_path):
    full = _chunk([[("u", "g|a|1.0|NA|jar")]])
    inc = _chunk([
        [("u", "g|b|1.0|NA|jar")],
        [("del", "g|a|1.0|NA|jar")],
    ])
    inc_name = "nexus-maven-repository-index.11.gz"
    base_props = _properties(last=10, retained=[10])
    target_props = _properties(last=11, timestamp="20260816110000.000 +0000", retained=[10, 11])
    routes = _routes(full, props=[FakeResponse(text=base_props), FakeResponse(text=target_props)])
    routes[MAVEN_INDEX_BASE + inc_name + ".sha1"] = FakeResponse(text=hashlib.sha1(inc).hexdigest())
    routes[MAVEN_INDEX_BASE + inc_name] = FakeResponse(content=inc)
    session = FakeSession(routes)
    queue = TechnologyHarvestQueue(tmp_path / "queue.sqlite3")
    try:
        harvester = MavenCentralIndexHarvester(queue=queue, user_agent="test", state_dir=tmp_path, session=session)
        boot = harvester.bootstrap(limit=0, reset=True)
        assert boot["complete"] is True
        assert boot["changes_cursor"] == 10

        partial = harvester.changes(limit=1)
        assert partial["target_complete"] is False
        assert partial["previous_cursor"] == 10
        assert partial["cursor"] == 10
        assert partial["inflight"] is not None

        finish = harvester.changes(limit=10)
        assert finish["target_complete"] is True
        assert finish["previous_cursor"] == 10
        assert finish["cursor"] == 11
        assert finish["inflight"] is None
        assert finish["registry_deleted_packages"] == 1
    finally:
        queue.close()


def test_maven_missing_incremental_chain_requires_rebootstrap_without_advancing(tmp_path):
    queue = TechnologyHarvestQueue(tmp_path / "queue.sqlite3")
    props = _properties(last=20, chain="chain-a", retained=[19, 20])
    session = FakeSession({MAVEN_INDEX_BASE + MAVEN_PROPERTIES: FakeResponse(text=props)})
    try:
        harvester = MavenCentralIndexHarvester(queue=queue, user_agent="test", state_dir=tmp_path, session=session)
        queue.set_cursor(MAVEN_BOOTSTRAP_SOURCE, cursor=json.dumps({"complete": True, "snapshot": {"chain_id": "chain-a", "timestamp": "T", "last_incremental": 10}}))
        queue.set_cursor(MAVEN_CHANGES_SOURCE, cursor=json.dumps({"chain_id": "chain-a", "timestamp": "T", "last_incremental": 10, "inflight": None}))
        result = harvester.changes(limit=100)
        assert result["rebootstrap_required"] is True
        assert 11 in result["missing_incrementals"]
        cursor = json.loads(queue.cursor(MAVEN_CHANGES_SOURCE)["cursor"])
        assert cursor["last_incremental"] == 10
    finally:
        queue.close()


def test_maven_native_authority_uses_repository_metadata_and_pom():
    name = "org.slf4j:slf4j-api"
    metadata_url = "https://repo1.maven.org/maven2/org/slf4j/slf4j-api/maven-metadata.xml"
    pom_url = "https://repo1.maven.org/maven2/org/slf4j/slf4j-api/2.0.17/slf4j-api-2.0.17.pom"
    metadata = b"""<metadata><groupId>org.slf4j</groupId><artifactId>slf4j-api</artifactId><versioning><latest>2.1.0-SNAPSHOT</latest><release>2.0.17</release><versions><version>2.0.16</version><version>2.0.17</version></versions></versioning></metadata>"""
    pom = b"""<project xmlns='http://maven.apache.org/POM/4.0.0'><url>https://www.slf4j.org</url><scm><url>https://github.com/qos-ch/slf4j</url></scm></project>"""
    session = FakeSession({metadata_url: FakeResponse(content=metadata), pom_url: FakeResponse(content=pom)})
    result = native_package_metadata(MAVEN_REGISTRY, name, session=session, user_agent="test")
    assert result is not None
    assert result["authority_source"] == "maven"
    assert result["latest_stable_version"] == "2.0.17"
    assert result["canonical_repository"] == "https://github.com/qos-ch/slf4j"
    assert build_purl(MAVEN_REGISTRY, name) == "pkg:maven/org.slf4j/slf4j-api"
