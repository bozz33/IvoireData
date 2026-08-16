from __future__ import annotations

from pathlib import Path

from .technology_maven import MavenCentralIndexHarvester as _BaseMavenCentralIndexHarvester


class MavenCentralIndexHarvester(_BaseMavenCentralIndexHarvester):
    """Runtime Maven harvester with bounded completed-chunk disk retention.

    The Maven Indexer full and incremental chunks can each be multi-gigabyte files. Once a
    chunk has been fully parsed and committed, it is not needed for the next chunk. Keep
    only the chunk currently being used; `.part` for the active download is deliberately
    preserved so HTTP Range resume remains possible after a budget cut or process restart.
    """

    def _prune_completed_chunks(self, *, keep_name: str) -> list[str]:
        removed: list[str] = []
        for path in self.cache_dir.glob("nexus-maven-repository-index*.gz"):
            if path.name == keep_name or not path.is_file():
                continue
            path.unlink(missing_ok=True)
            removed.append(path.name)
        return sorted(removed)

    def _download_chunk(self, chunk_name: str, expected_sha1: str) -> tuple[Path, str, int]:
        # Previous chunks are already transactionally applied before the state machine is
        # allowed to request the next chunk. It is therefore safe to discard completed
        # predecessors before downloading/resuming the current one.
        self._prune_completed_chunks(keep_name=chunk_name)
        return super()._download_chunk(chunk_name, expected_sha1)
