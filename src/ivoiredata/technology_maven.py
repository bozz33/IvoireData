from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterable
from urllib.parse import urljoin

import requests

from .http_client import new_session
from .technology_harvester import HarvestCandidate, _now


MAVEN_REGISTRY = "repo1.maven.org"
MAVEN_INDEX_BASE = "https://repo1.maven.org/maven2/.index/"
MAVEN_PROPERTIES = "nexus-maven-repository-index.properties"
MAVEN_FULL_CHUNK = "nexus-maven-repository-index.gz"
MAVEN_BOOTSTRAP_SOURCE = "maven-central-index-bootstrap"
MAVEN_CHANGES_SOURCE = "maven-central-index-changes"

_INDEX_PREFIX = "nexus-maven-repository-index"
_SHA1_RE = re.compile(r"\b([0-9a-fA-F]{40})\b")


def _cursor_json(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _dump_cursor(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_properties(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "!")):
            continue
        match = re.match(r"([^:=\s]+)\s*[:=]\s*(.*)$", line)
        if match:
            out[match.group(1).strip()] = match.group(2).strip()
            continue
        parts = line.split(None, 1)
        if parts:
            out[parts[0]] = parts[1].strip() if len(parts) > 1 else ""
    return out


def _snapshot_from_properties(text: str) -> dict[str, Any]:
    props = _parse_properties(text)
    index_id = props.get("nexus.index.id", "").strip()
    chain_id = props.get("nexus.index.chain-id", "").strip()
    timestamp = props.get("nexus.index.timestamp", "").strip()
    raw_last = props.get("nexus.index.last-incremental", "").strip()
    if not index_id or not chain_id or not timestamp:
        raise ValueError("Maven Central index properties are missing id/chain-id/timestamp")
    try:
        last_incremental = int(raw_last)
    except ValueError as exc:
        raise ValueError("Maven Central index properties have invalid last-incremental") from exc

    incrementals: dict[int, int] = {}
    for key, value in props.items():
        if not key.startswith("nexus.index.incremental-"):
            continue
        try:
            slot = int(key.rsplit("-", 1)[1])
            chunk = int(value)
        except ValueError:
            continue
        incrementals[slot] = chunk

    return {
        "index_id": index_id,
        "chain_id": chain_id,
        "timestamp": timestamp,
        "last_incremental": last_incremental,
        "incrementals": sorted(set(incrementals.values())),
        "properties_sha256": hashlib.sha256(text.encode("iso-8859-1", "replace")).hexdigest(),
    }


def _decode_modified_utf(data: bytes) -> str:
    units: list[int] = []
    i = 0
    n = len(data)
    while i < n:
        c = data[i]
        if c >> 7 == 0:
            units.append(c)
            i += 1
        elif (c >> 4) in (0xC, 0xD):
            if i + 1 >= n or data[i + 1] & 0xC0 != 0x80:
                raise UnicodeDecodeError("modified-utf8", data, i, min(i + 2, n), "malformed two-byte sequence")
            units.append(((c & 0x1F) << 6) | (data[i + 1] & 0x3F))
            i += 2
        elif c >> 4 == 0xE:
            if i + 2 >= n or data[i + 1] & 0xC0 != 0x80 or data[i + 2] & 0xC0 != 0x80:
                raise UnicodeDecodeError("modified-utf8", data, i, min(i + 3, n), "malformed three-byte sequence")
            units.append(((c & 0x0F) << 12) | ((data[i + 1] & 0x3F) << 6) | (data[i + 2] & 0x3F))
            i += 3
        else:
            raise UnicodeDecodeError("modified-utf8", data, i, i + 1, "unsupported byte")
    raw = b"".join(struct.pack("<H", unit) for unit in units)
    return raw.decode("utf-16-le", "surrogatepass")


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    if size < 0:
        raise ValueError("negative Maven index field length")
    data = stream.read(size)
    if len(data) != size:
        raise EOFError("truncated Maven index chunk")
    return data


def _read_u16_utf(stream: BinaryIO) -> str:
    size = struct.unpack(">H", _read_exact(stream, 2))[0]
    return _decode_modified_utf(_read_exact(stream, size))


def _read_i32_utf(stream: BinaryIO, *, decode: bool = True) -> str | None:
    size = struct.unpack(">i", _read_exact(stream, 4))[0]
    raw = _read_exact(stream, size)
    return _decode_modified_utf(raw) if decode else None


@dataclass(frozen=True)
class MavenIndexEvent:
    ordinal: int
    kind: str
    uinfo: str
    modified: int | None


class MavenChunkReader:
    """Pure-Python reader for the Maven Indexer transport chunk format."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.version: int | None = None
        self.timestamp_ms: int | None = None

    def events(self, *, start_ordinal: int = 0) -> Iterable[MavenIndexEvent]:
        with self.path.open("rb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="rb") as stream:
                self.version = _read_exact(stream, 1)[0]
                if self.version != 1:
                    raise ValueError(f"unsupported Maven index chunk version {self.version}")
                self.timestamp_ms = struct.unpack(">q", _read_exact(stream, 8))[0]
                ordinal = 0
                while True:
                    first = stream.read(4)
                    if not first:
                        break
                    if len(first) != 4:
                        raise EOFError("truncated Maven index record header")
                    field_count = struct.unpack(">i", first)[0]
                    if field_count < 0 or field_count > 100_000:
                        raise ValueError(f"invalid Maven index field count {field_count}")
                    selected: dict[str, str] = {}
                    for _ in range(field_count):
                        _read_exact(stream, 1)
                        name = _read_u16_utf(stream)
                        keep = name in {"u", "del", "m"}
                        value = _read_i32_utf(stream, decode=keep)
                        if keep and value is not None:
                            selected[name] = value
                    ordinal += 1
                    if ordinal <= int(start_ordinal):
                        continue
                    uinfo = selected.get("del")
                    kind = "REMOVE" if uinfo else "ADD"
                    if not uinfo:
                        uinfo = selected.get("u")
                    if not uinfo:
                        continue
                    modified = None
                    raw_modified = selected.get("m")
                    if raw_modified:
                        try:
                            modified = int(raw_modified)
                        except ValueError:
                            modified = None
                    yield MavenIndexEvent(ordinal=ordinal, kind=kind, uinfo=uinfo, modified=modified)


def _uinfo_parts(uinfo: str) -> tuple[str, str, str, str, str] | None:
    parts = str(uinfo or "").split("|")
    if len(parts) < 5:
        return None
    group_id, artifact_id, version, classifier, extension = parts[:5]
    if not group_id or not artifact_id or not version:
        return None
    return group_id, artifact_id, version, classifier, extension


def _sha1_from_text(text: str) -> str:
    match = _SHA1_RE.search(str(text or ""))
    if not match:
        raise ValueError("Maven Central index checksum did not contain a SHA-1")
    return match.group(1).lower()


@dataclass
class MavenIndexStats:
    processed_artifacts: int = 0
    artifact_adds: int = 0
    artifact_removes: int = 0
    replayed_artifacts: int = 0
    ignored_records: int = 0
    inserted_packages: int = 0
    updated_packages: int = 0
    deleted_packages: int = 0
    pages_fetched: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "processed_artifacts": self.processed_artifacts,
            "artifact_adds": self.artifact_adds,
            "artifact_removes": self.artifact_removes,
            "replayed_artifacts": self.replayed_artifacts,
            "ignored_records": self.ignored_records,
            "inserted_packages": self.inserted_packages,
            "updated_packages": self.updated_packages,
            "deleted_packages": self.deleted_packages,
            "pages_fetched": self.pages_fetched,
        }


class MavenCentralIndexHarvester:
    """Exhaustive + incremental Central discovery through the official Maven Indexer feed."""

    def __init__(self, *, queue: Any, user_agent: str, state_dir: Path, session: requests.Session | None = None) -> None:
        self.queue = queue
        self.user_agent = user_agent
        self.state_dir = Path(state_dir)
        self.cache_dir = self.state_dir / "maven_index"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = session or new_session(user_agent)
        self._init_schema()

    def _init_schema(self) -> None:
        self.queue.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS maven_package_state (
                package_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS maven_version_state (
                version_id INTEGER PRIMARY KEY,
                package_id INTEGER NOT NULL,
                version TEXT NOT NULL,
                live_artifacts INTEGER NOT NULL DEFAULT 0,
                last_modified INTEGER,
                UNIQUE(package_id, version)
            );
            CREATE INDEX IF NOT EXISTS idx_maven_version_package_live
                ON maven_version_state(package_id, live_artifacts);
            CREATE TABLE IF NOT EXISTS maven_artifact_state (
                uinfo_sha256 BLOB PRIMARY KEY,
                version_id INTEGER NOT NULL,
                live INTEGER NOT NULL,
                last_modified INTEGER
            ) WITHOUT ROWID;
            """
        )
        self.queue.db.commit()

    def _headers(self, **extra: str) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent}
        headers.update(extra)
        return headers

    def _get_text(self, name: str) -> str:
        response = self.session.get(urljoin(MAVEN_INDEX_BASE, name), headers=self._headers(Accept="text/plain,*/*;q=0.1"), timeout=120)
        response.raise_for_status()
        return str(response.text or "")

    def _remote_snapshot(self) -> dict[str, Any]:
        text = self._get_text(MAVEN_PROPERTIES)
        snapshot = _snapshot_from_properties(text)
        snapshot["properties_text"] = text
        return snapshot

    def _checksum(self, chunk_name: str) -> str:
        return _sha1_from_text(self._get_text(chunk_name + ".sha1"))

    def _download_chunk(self, chunk_name: str, expected_sha1: str) -> tuple[Path, str, int]:
        final = self.cache_dir / chunk_name
        part = self.cache_dir / (chunk_name + ".part")

        def verified(path: Path) -> tuple[bool, str, int]:
            sha1 = hashlib.sha1()
            sha256 = hashlib.sha256()
            size = 0
            with path.open("rb") as fh:
                while True:
                    block = fh.read(1024 * 1024)
                    if not block:
                        break
                    sha1.update(block)
                    sha256.update(block)
                    size += len(block)
            return sha1.hexdigest().lower() == expected_sha1.lower(), sha256.hexdigest(), size

        if final.exists():
            ok, digest, size = verified(final)
            if ok:
                return final, digest, size
            final.unlink()

        offset = part.stat().st_size if part.exists() else 0
        headers = self._headers(Accept="application/octet-stream")
        if offset:
            headers["Range"] = f"bytes={offset}-"
        response = self.session.get(urljoin(MAVEN_INDEX_BASE, chunk_name), headers=headers, timeout=300, stream=True)
        if offset and response.status_code == 200:
            offset = 0
            part.unlink(missing_ok=True)
        elif offset and response.status_code != 206:
            response.raise_for_status()
        else:
            response.raise_for_status()

        mode = "ab" if offset and response.status_code == 206 else "wb"
        with part.open(mode) as fh:
            for block in response.iter_content(chunk_size=1024 * 1024):
                if block:
                    fh.write(block)
            fh.flush()
            os.fsync(fh.fileno())

        ok, digest, size = verified(part)
        if not ok:
            raise ValueError(f"Maven index SHA-1 mismatch for {chunk_name}")
        os.replace(part, final)
        return final, digest, size

    def _package_id(self, name: str) -> int:
        self.queue.db.execute("INSERT OR IGNORE INTO maven_package_state(name) VALUES(?)", (name,))
        row = self.queue.db.execute("SELECT package_id FROM maven_package_state WHERE name=?", (name,)).fetchone()
        return int(row["package_id"])

    def _version_id(self, package_id: int, version: str, modified: int | None) -> int:
        self.queue.db.execute(
            """
            INSERT INTO maven_version_state(package_id,version,live_artifacts,last_modified)
            VALUES(?,?,0,?)
            ON CONFLICT(package_id,version) DO UPDATE SET
                last_modified=CASE
                    WHEN excluded.last_modified IS NULL THEN maven_version_state.last_modified
                    WHEN maven_version_state.last_modified IS NULL THEN excluded.last_modified
                    ELSE MAX(maven_version_state.last_modified, excluded.last_modified)
                END
            """,
            (package_id, version, modified),
        )
        row = self.queue.db.execute("SELECT version_id FROM maven_version_state WHERE package_id=? AND version=?", (package_id, version)).fetchone()
        return int(row["version_id"])

    def _package_live(self, package_id: int) -> bool:
        row = self.queue.db.execute("SELECT 1 FROM maven_version_state WHERE package_id=? AND live_artifacts>0 LIMIT 1", (package_id,)).fetchone()
        return row is not None

    def _apply_events(self, *, events: Iterable[MavenIndexEvent], source: str, cursor_payload: dict[str, Any], requeue_changed: bool) -> MavenIndexStats:
        stats = MavenIndexStats()
        touched: dict[int, str] = {}
        previous_status: dict[str, str | None] = {}
        with self.queue.db:
            for event in events:
                parsed = _uinfo_parts(event.uinfo)
                if parsed is None:
                    stats.ignored_records += 1
                    continue
                group_id, artifact_id, version, _classifier, _extension = parsed
                name = f"{group_id}:{artifact_id}"
                package_id = self._package_id(name)
                version_id = self._version_id(package_id, version, event.modified)
                artifact_hash = hashlib.sha256(event.uinfo.encode("utf-8")).digest()
                existing = self.queue.db.execute("SELECT live FROM maven_artifact_state WHERE uinfo_sha256=?", (artifact_hash,)).fetchone()
                was_live = bool(existing and int(existing["live"]))
                want_live = event.kind == "ADD"

                if existing is None:
                    self.queue.db.execute("INSERT INTO maven_artifact_state(uinfo_sha256,version_id,live,last_modified) VALUES(?,?,?,?)", (artifact_hash, version_id, 1 if want_live else 0, event.modified))
                    changed = want_live
                elif was_live != want_live:
                    self.queue.db.execute("UPDATE maven_artifact_state SET version_id=?,live=?,last_modified=? WHERE uinfo_sha256=?", (version_id, 1 if want_live else 0, event.modified, artifact_hash))
                    changed = True
                else:
                    changed = False
                    stats.replayed_artifacts += 1

                if changed:
                    delta = 1 if want_live else -1
                    self.queue.db.execute(
                        """
                        UPDATE maven_version_state
                        SET live_artifacts=MAX(0,live_artifacts+?),
                            last_modified=CASE
                                WHEN ? IS NULL THEN last_modified
                                WHEN last_modified IS NULL THEN ?
                                ELSE MAX(last_modified,?)
                            END
                        WHERE version_id=?
                        """,
                        (delta, event.modified, event.modified, event.modified, version_id),
                    )

                stats.processed_artifacts += 1
                if event.kind == "ADD":
                    stats.artifact_adds += 1
                else:
                    stats.artifact_removes += 1
                touched[package_id] = name
                if name not in previous_status:
                    row = self.queue.db.execute("SELECT status FROM candidates WHERE registry=? AND name=?", (MAVEN_REGISTRY, name)).fetchone()
                    previous_status[name] = str(row["status"]) if row else None

            now = _now()
            snapshot = cursor_payload.get("snapshot") if isinstance(cursor_payload.get("snapshot"), dict) else {}
            inflight = cursor_payload.get("inflight") if isinstance(cursor_payload.get("inflight"), dict) else {}
            token = str(snapshot.get("timestamp") or inflight.get("target_timestamp") or "") or None
            for package_id, name in touched.items():
                live = self._package_live(package_id)
                prev = previous_status.get(name)
                if not live:
                    self.queue._mark_deleted_no_commit(MAVEN_REGISTRY, name, source=source, now=now)
                    if prev != "DELETED":
                        stats.deleted_packages += 1
                    continue
                candidate = HarvestCandidate(MAVEN_REGISTRY, name, source, 65 if requeue_changed else 25, requeue=bool(requeue_changed or prev == "DELETED"), seen_token=token)
                if self.queue._upsert_one(candidate, now=now):
                    stats.inserted_packages += 1
                else:
                    stats.updated_packages += 1
            self.queue._set_cursor_no_commit(source, cursor=_dump_cursor(cursor_payload))
        return stats

    def _registry_stats(self) -> dict[str, int]:
        packages = self.queue.db.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN status='DELETED' THEN 1 ELSE 0 END) AS deleted FROM candidates WHERE registry=?", (MAVEN_REGISTRY,)).fetchone()
        versions = self.queue.db.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN live_artifacts=0 THEN 1 ELSE 0 END) AS deleted FROM maven_version_state").fetchone()
        artifacts = self.queue.db.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN live=1 THEN 1 ELSE 0 END) AS live FROM maven_artifact_state").fetchone()
        return {
            "registry_candidates": int(packages["total"] or 0),
            "registry_deleted_packages": int(packages["deleted"] or 0),
            "version_states": int(versions["total"] or 0),
            "deleted_versions": int(versions["deleted"] or 0),
            "artifact_states": int(artifacts["total"] or 0),
            "live_artifacts": int(artifacts["live"] or 0),
        }

    def _clear_registry(self) -> None:
        self.queue.reset_cursor(MAVEN_BOOTSTRAP_SOURCE)
        self.queue.reset_cursor(MAVEN_CHANGES_SOURCE)
        with self.queue.db:
            self.queue.db.execute("DELETE FROM maven_artifact_state")
            self.queue.db.execute("DELETE FROM maven_version_state")
            self.queue.db.execute("DELETE FROM maven_package_state")
            self.queue.db.execute("DELETE FROM candidates WHERE registry=?", (MAVEN_REGISTRY,))

    def _parse_chunk_bounded(self, *, path: Path, start_ordinal: int, limit: int) -> tuple[list[MavenIndexEvent], int, bool]:
        events: list[MavenIndexEvent] = []
        last_ordinal = start_ordinal
        exhausted = True
        target = None if int(limit) <= 0 else max(1, int(limit))
        reader = MavenChunkReader(path)
        iterator = iter(reader.events(start_ordinal=start_ordinal))
        while target is None or len(events) < target:
            try:
                event = next(iterator)
            except StopIteration:
                break
            events.append(event)
            last_ordinal = event.ordinal
        if target is not None and len(events) >= target:
            try:
                next(iterator)
            except StopIteration:
                exhausted = True
            else:
                exhausted = False
        return events, last_ordinal, exhausted

    def bootstrap(self, *, limit: int = 500, reset: bool = False) -> dict[str, Any]:
        if reset:
            self._clear_registry()

        state = _cursor_json(self.queue.cursor(MAVEN_BOOTSTRAP_SOURCE).get("cursor"))
        if state.get("complete"):
            changes = _cursor_json(self.queue.cursor(MAVEN_CHANGES_SOURCE).get("cursor"))
            return {
                "source": MAVEN_BOOTSTRAP_SOURCE,
                "registry": MAVEN_REGISTRY,
                "full": True,
                "complete": True,
                "snapshot": state.get("snapshot"),
                "changes_cursor": changes.get("last_incremental"),
                "processed_artifacts": 0,
                "pages_fetched": 0,
                "http_work_required": False,
                **self._registry_stats(),
            }

        if not state:
            snapshot = self._remote_snapshot()
            expected_sha1 = self._checksum(MAVEN_FULL_CHUNK)
            state = {
                "complete": False,
                "phase": "DOWNLOAD",
                "snapshot": {key: snapshot[key] for key in ("index_id", "chain_id", "timestamp", "last_incremental", "incrementals", "properties_sha256")},
                "chunk_name": MAVEN_FULL_CHUNK,
                "expected_sha1": expected_sha1,
                "chunk_sha256": None,
                "chunk_size": None,
                "raw_record_ordinal": 0,
            }
            self.queue.set_cursor(MAVEN_BOOTSTRAP_SOURCE, cursor=_dump_cursor(state))

        chunk_name = str(state.get("chunk_name") or MAVEN_FULL_CHUNK)
        expected_sha1 = str(state.get("expected_sha1") or "").strip()
        if not expected_sha1:
            raise RuntimeError("Maven bootstrap cursor is missing expected SHA-1")
        path, sha256, size = self._download_chunk(chunk_name, expected_sha1)
        if state.get("phase") != "PARSE" or state.get("chunk_sha256") != sha256:
            state = {**state, "phase": "PARSE", "chunk_sha256": sha256, "chunk_size": size}
            self.queue.set_cursor(MAVEN_BOOTSTRAP_SOURCE, cursor=_dump_cursor(state))

        events, last_ordinal, exhausted = self._parse_chunk_bounded(path=path, start_ordinal=int(state.get("raw_record_ordinal") or 0), limit=limit)
        next_state = {**state, "raw_record_ordinal": last_ordinal}
        stats = self._apply_events(events=events, source=MAVEN_BOOTSTRAP_SOURCE, cursor_payload=next_state, requeue_changed=False)
        if exhausted:
            next_state = {**next_state, "complete": True, "phase": "COMPLETE"}
            snapshot = next_state["snapshot"]
            changes = {"chain_id": snapshot["chain_id"], "timestamp": snapshot["timestamp"], "last_incremental": int(snapshot["last_incremental"]), "inflight": None}
            with self.queue.db:
                self.queue._set_cursor_no_commit(MAVEN_BOOTSTRAP_SOURCE, cursor=_dump_cursor(next_state))
                self.queue._set_cursor_no_commit(MAVEN_CHANGES_SOURCE, cursor=_dump_cursor(changes))

        return {
            "source": MAVEN_BOOTSTRAP_SOURCE,
            "registry": MAVEN_REGISTRY,
            "full": True,
            "complete": exhausted,
            "snapshot": next_state.get("snapshot"),
            "raw_record_ordinal": next_state.get("raw_record_ordinal"),
            "changes_cursor": _cursor_json(self.queue.cursor(MAVEN_CHANGES_SOURCE).get("cursor")).get("last_incremental"),
            **stats.as_dict(),
            **self._registry_stats(),
        }

    def _new_inflight(self, changes: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        remote = self._remote_snapshot()
        base_chain = str(changes.get("chain_id") or "")
        base_last = int(changes.get("last_incremental") or -1)
        target_last = int(remote["last_incremental"])
        if remote["chain_id"] != base_chain:
            return changes, {"rebootstrap_required": True, "reason": "Maven Central index chain-id changed", "remote_chain_id": remote["chain_id"]}
        if target_last <= base_last:
            return changes, None

        available = set(int(v) for v in remote.get("incrementals") or [])
        required = list(range(base_last + 1, target_last + 1))
        missing = [value for value in required if value not in available]
        if missing:
            return changes, {"rebootstrap_required": True, "reason": "required Maven incremental chunks are no longer retained upstream", "missing_incrementals": missing[:100]}

        inflight = {
            "base_last_incremental": base_last,
            "target_last_incremental": target_last,
            "target_timestamp": remote["timestamp"],
            "chain_id": remote["chain_id"],
            "chunk_number": required[0],
            "raw_record_ordinal": 0,
            "chunk_name": None,
            "expected_sha1": None,
            "chunk_sha256": None,
        }
        changes = {**changes, "inflight": inflight}
        self.queue.set_cursor(MAVEN_CHANGES_SOURCE, cursor=_dump_cursor(changes))
        return changes, None

    def changes(self, *, limit: int = 500, reset: bool = False) -> dict[str, Any]:
        bootstrap = _cursor_json(self.queue.cursor(MAVEN_BOOTSTRAP_SOURCE).get("cursor"))
        if reset:
            self.queue.reset_cursor(MAVEN_CHANGES_SOURCE)

        if not bootstrap.get("complete"):
            return {
                "source": MAVEN_CHANGES_SOURCE,
                "registry": MAVEN_REGISTRY,
                "full": False,
                "bootstrap_required": True,
                "complete_bootstrap": False,
                "processed_artifacts": 0,
                "inserted_packages": 0,
                "updated_packages": 0,
                "deleted_packages": 0,
                "http_work_required": False,
            }

        changes = _cursor_json(self.queue.cursor(MAVEN_CHANGES_SOURCE).get("cursor"))
        if not changes:
            snapshot = bootstrap.get("snapshot") or {}
            changes = {"chain_id": snapshot.get("chain_id"), "timestamp": snapshot.get("timestamp"), "last_incremental": int(snapshot.get("last_incremental") or -1), "inflight": None}
            self.queue.set_cursor(MAVEN_CHANGES_SOURCE, cursor=_dump_cursor(changes))

        if not isinstance(changes.get("inflight"), dict):
            changes, issue = self._new_inflight(changes)
            if issue:
                return {"source": MAVEN_CHANGES_SOURCE, "registry": MAVEN_REGISTRY, "full": False, "bootstrap_required": False, **issue, "processed_artifacts": 0, "deleted_packages": 0, **self._registry_stats()}
            if not isinstance(changes.get("inflight"), dict):
                return {
                    "source": MAVEN_CHANGES_SOURCE,
                    "registry": MAVEN_REGISTRY,
                    "full": False,
                    "bootstrap_required": False,
                    "previous_cursor": changes.get("last_incremental"),
                    "cursor": changes.get("last_incremental"),
                    "target_complete": True,
                    "inflight": None,
                    "processed_artifacts": 0,
                    "pages_fetched": 0,
                    "deleted_packages": 0,
                    **self._registry_stats(),
                }

        target = None if int(limit) <= 0 else max(1, int(limit))
        aggregate = MavenIndexStats()
        previous_cursor = int(changes.get("last_incremental") or -1)

        while target is None or aggregate.processed_artifacts < target:
            inflight = changes.get("inflight")
            if not isinstance(inflight, dict):
                break
            chunk_number = int(inflight["chunk_number"])
            target_last = int(inflight["target_last_incremental"])
            chunk_name = f"{_INDEX_PREFIX}.{chunk_number}.gz"

            if inflight.get("chunk_name") != chunk_name or not inflight.get("expected_sha1"):
                expected_sha1 = self._checksum(chunk_name)
                inflight = {**inflight, "chunk_name": chunk_name, "expected_sha1": expected_sha1, "chunk_sha256": None, "raw_record_ordinal": int(inflight.get("raw_record_ordinal") or 0)}
                changes = {**changes, "inflight": inflight}
                self.queue.set_cursor(MAVEN_CHANGES_SOURCE, cursor=_dump_cursor(changes))

            path, sha256, _size = self._download_chunk(chunk_name, str(inflight["expected_sha1"]))
            if inflight.get("chunk_sha256") != sha256:
                inflight = {**inflight, "chunk_sha256": sha256}
                changes = {**changes, "inflight": inflight}
                self.queue.set_cursor(MAVEN_CHANGES_SOURCE, cursor=_dump_cursor(changes))

            remaining = 0 if target is None else target - aggregate.processed_artifacts
            events, last_ordinal, exhausted = self._parse_chunk_bounded(path=path, start_ordinal=int(inflight.get("raw_record_ordinal") or 0), limit=remaining if target is not None else 0)
            next_inflight = {**inflight, "raw_record_ordinal": last_ordinal}
            next_changes = {**changes, "inflight": next_inflight}
            stats = self._apply_events(events=events, source=MAVEN_CHANGES_SOURCE, cursor_payload=next_changes, requeue_changed=True)
            for field, value in stats.as_dict().items():
                setattr(aggregate, field, getattr(aggregate, field) + value)
            changes = next_changes

            if not exhausted:
                break

            if chunk_number >= target_last:
                changes = {**changes, "chain_id": inflight["chain_id"], "timestamp": inflight["target_timestamp"], "last_incremental": target_last, "inflight": None}
                self.queue.set_cursor(MAVEN_CHANGES_SOURCE, cursor=_dump_cursor(changes))
                break

            next_inflight = {**inflight, "chunk_number": chunk_number + 1, "raw_record_ordinal": 0, "chunk_name": None, "expected_sha1": None, "chunk_sha256": None}
            changes = {**changes, "inflight": next_inflight}
            self.queue.set_cursor(MAVEN_CHANGES_SOURCE, cursor=_dump_cursor(changes))
            if target is not None and aggregate.processed_artifacts >= target:
                break

        complete_target = not isinstance(changes.get("inflight"), dict)
        return {
            "source": MAVEN_CHANGES_SOURCE,
            "registry": MAVEN_REGISTRY,
            "full": False,
            "bootstrap_required": False,
            "previous_cursor": previous_cursor,
            "cursor": changes.get("last_incremental"),
            "target_complete": complete_target,
            "inflight": changes.get("inflight"),
            **aggregate.as_dict(),
            **self._registry_stats(),
        }

    def harvest(self, *, full: bool = False, limit: int = 500, reset: bool = False) -> dict[str, Any]:
        return self.bootstrap(limit=limit, reset=reset) if full else self.changes(limit=limit, reset=reset)
