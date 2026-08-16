from __future__ import annotations

import gzip
import hashlib
import io
import struct

from ivoiredata.technology_harvester import TechnologyHarvestQueue
from ivoiredata.technology_maven import (
    MAVEN_BOOTSTRAP_SOURCE,
    MAVEN_FULL_CHUNK,
    MAVEN_INDEX_BASE,
    MAVEN_PROPERTIES,
    MavenCentralIndexHarvester,
)


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


def _chunk(count: int) -> bytes:
    payload = bytearray(b"\x01" + struct.pack(">q", 1_700_000_000_000))
    for i in range(count):
        record = [("u", f"org.example|artifact-{i % 7}|1.{i}|NA|jar")]
        payload.extend(struct.pack(">i", len(record)))
        for name, value in record:
            payload.extend(_field(name, value))
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as gz:
        gz.write(payload)
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, *, text="", content=None, status_code=200):
        self.text = text
        self.content = content if content is not None else text.encode()
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), max(1, chunk_size)):
            yield self.content[i:i + chunk_size]


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.routes[url]


def test_maven_unbounded_bootstrap_commits_internal_batches(tmp_path):
    full = _chunk(25)
    props = "\n".join([
        "nexus.index.id=central",
        "nexus.index.chain-id=chain-a",
        "nexus.index.timestamp=20260816100000.000 +0000",
        "nexus.index.last-incremental=10",
        "nexus.index.incremental-0=10",
        "",
    ])
    routes = {
        MAVEN_INDEX_BASE + MAVEN_PROPERTIES: FakeResponse(text=props),
        MAVEN_INDEX_BASE + MAVEN_FULL_CHUNK + ".sha1": FakeResponse(text=hashlib.sha1(full).hexdigest()),
        MAVEN_INDEX_BASE + MAVEN_FULL_CHUNK: FakeResponse(content=full),
    }
    queue = TechnologyHarvestQueue(tmp_path / "queue.sqlite3")
    try:
        h = MavenCentralIndexHarvester(
            queue=queue,
            user_agent="test",
            state_dir=tmp_path,
            session=FakeSession(routes),
            parse_commit_batch_size=4,
        )
        commits: list[int] = []
        original = h._apply_events

        def observed(*, events, source, cursor_payload, requeue_changed):
            materialized = list(events)
            assert len(materialized) <= 4
            stats = original(
                events=materialized,
                source=source,
                cursor_payload=cursor_payload,
                requeue_changed=requeue_changed,
            )
            if source == MAVEN_BOOTSTRAP_SOURCE:
                commits.append(int(cursor_payload["raw_record_ordinal"]))
            return stats

        h._apply_events = observed  # type: ignore[method-assign]
        result = h.bootstrap(limit=0, reset=True)
        assert result["complete"] is True
        assert result["processed_artifacts"] == 25
        assert result["raw_record_ordinal"] == 25
        assert commits == [4, 8, 12, 16, 20, 24, 25]
    finally:
        queue.close()
