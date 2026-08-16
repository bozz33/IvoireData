from __future__ import annotations

import hashlib

from ivoiredata.technology_harvester import TechnologyHarvestQueue
from ivoiredata.technology_maven import MAVEN_INDEX_BASE
from ivoiredata.technology_maven_runtime import MavenCentralIndexHarvester


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1):
        for offset in range(0, len(self.content), max(1, chunk_size)):
            yield self.content[offset : offset + chunk_size]


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_maven_runtime_resumes_part_and_prunes_previous_completed_chunk(tmp_path):
    queue = TechnologyHarvestQueue(tmp_path / "queue.sqlite3")
    try:
        payload = b"maven-index-payload"
        split = 7
        session = FakeSession(FakeResponse(payload[split:], status_code=206))
        harvester = MavenCentralIndexHarvester(
            queue=queue,
            user_agent="test",
            state_dir=tmp_path,
            session=session,
        )
        chunk_name = "nexus-maven-repository-index.42.gz"
        previous = harvester.cache_dir / "nexus-maven-repository-index.gz"
        previous.write_bytes(b"old-full")
        part = harvester.cache_dir / (chunk_name + ".part")
        part.write_bytes(payload[:split])

        final, digest, size = harvester._download_chunk(
            chunk_name,
            hashlib.sha1(payload).hexdigest(),
        )

        assert final.read_bytes() == payload
        assert size == len(payload)
        assert digest == hashlib.sha256(payload).hexdigest()
        assert not part.exists()
        assert not previous.exists()
        assert session.calls == [
            (
                MAVEN_INDEX_BASE + chunk_name,
                {
                    "headers": {
                        "User-Agent": "test",
                        "Accept": "application/octet-stream",
                        "Range": f"bytes={split}-",
                    },
                    "timeout": 300,
                    "stream": True,
                },
            )
        ]
    finally:
        queue.close()
