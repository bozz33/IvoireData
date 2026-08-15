from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator


CRATES_INDEX_URL = "https://github.com/rust-lang/crates.io-index"
CRATES_BOOTSTRAP_SOURCE = "crates-index-bootstrap"
CRATES_CHANGES_SOURCE = "crates-index-changes"
CRATES_REGISTRY = "crates.io"
_BATCH_SIZE = 5000
_CRATE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


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


def crate_name_from_index_path(path: str) -> str | None:
    """Return the normalized crate name encoded by an official Cargo index path.

    The Cargo index deliberately stores filenames in lowercase. Canonical display case is
    recovered later from the crates.io API during qualification.
    """
    value = str(path or "").strip().strip("/")
    if not value or value == "config.json" or value.startswith("."):
        return None
    parts = value.split("/")
    valid_shape = False
    if len(parts) == 2 and parts[0] in {"1", "2"}:
        valid_shape = True
    elif len(parts) == 3 and parts[0] == "3" and len(parts[1]) == 1:
        valid_shape = True
    elif len(parts) == 3 and len(parts[0]) == 2 and len(parts[1]) == 2:
        valid_shape = True
    if not valid_shape:
        return None
    name = parts[-1]
    if not _CRATE_NAME.fullmatch(name):
        return None
    return name


@dataclass
class GitMetrics:
    commands: int = 0
    network_operations: int = 0
    clone_performed: bool = False
    fetch_performed: bool = False
    paths_scanned: int = 0
    diff_entries: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "commands": self.commands,
            "network_operations": self.network_operations,
            "clone_performed": self.clone_performed,
            "fetch_performed": self.fetch_performed,
            "paths_scanned": self.paths_scanned,
            "diff_entries": self.diff_entries,
        }


class GitIndexClient:
    """Minimal partial-clone client for the official crates.io Git index."""

    def __init__(self, repo_path: Path):
        self.repo_path = Path(repo_path)
        self.metrics = GitMetrics()

    @property
    def exists(self) -> bool:
        return (self.repo_path / ".git").is_dir()

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        network: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if shutil.which("git") is None:
            raise RuntimeError("git executable is required for the crates.io index harvester")
        self.metrics.commands += 1
        if network:
            self.metrics.network_operations += 1
        env = dict(os.environ)
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        process = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and process.returncode != 0:
            detail = (process.stderr or process.stdout or "git command failed").strip()
            raise RuntimeError(f"git {' '.join(args)} failed: {detail[-2000:]}")
        return process

    def ensure_clone(self) -> None:
        if self.exists:
            return
        self.repo_path.parent.mkdir(parents=True, exist_ok=True)
        if self.repo_path.exists():
            raise RuntimeError(
                f"crates.io index path exists but is not a git repository: {self.repo_path}"
            )
        self._run(
            [
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                "--single-branch",
                CRATES_INDEX_URL,
                str(self.repo_path),
            ],
            network=True,
        )
        self.metrics.clone_performed = True

    def fetch(self) -> None:
        if not self.exists:
            self.ensure_clone()
            return
        self._run(["fetch", "--prune", "origin"], cwd=self.repo_path, network=True)
        self.metrics.fetch_performed = True

    def remote_ref(self) -> str:
        symbolic = self._run(
            ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
            cwd=self.repo_path,
            check=False,
        )
        value = symbolic.stdout.strip()
        if symbolic.returncode == 0 and value:
            return value
        for ref in ("refs/remotes/origin/master", "refs/remotes/origin/main"):
            probe = self._run(["rev-parse", "--verify", ref], cwd=self.repo_path, check=False)
            if probe.returncode == 0:
                return ref
        raise RuntimeError("unable to resolve crates.io index default remote branch")

    def remote_head(self) -> str:
        ref = self.remote_ref()
        return self._run(["rev-parse", "--verify", ref], cwd=self.repo_path).stdout.strip()

    def iter_paths(self, commit: str) -> Iterator[str]:
        if shutil.which("git") is None:
            raise RuntimeError("git executable is required for the crates.io index harvester")
        self.metrics.commands += 1
        env = dict(os.environ)
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        process = subprocess.Popen(
            ["git", "ls-tree", "-r", "--name-only", commit],
            cwd=str(self.repo_path),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        try:
            for line in process.stdout:
                path = line.rstrip("\r\n")
                if path:
                    self.metrics.paths_scanned += 1
                    yield path
        finally:
            process.stdout.close()
        stderr = process.stderr.read() if process.stderr is not None else ""
        if process.stderr is not None:
            process.stderr.close()
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(f"git ls-tree failed: {stderr.strip()[-2000:]}")

    def diff_entries(self, old_commit: str, new_commit: str) -> list[tuple[str, str, str | None]]:
        process = self._run(
            ["diff", "--name-status", "-z", "--find-renames", old_commit, new_commit, "--"],
            cwd=self.repo_path,
        )
        tokens = process.stdout.split("\0")
        entries: list[tuple[str, str, str | None]] = []
        index = 0
        while index < len(tokens):
            status = tokens[index]
            index += 1
            if not status:
                continue
            if index >= len(tokens):
                raise RuntimeError("malformed git diff --name-status output")
            old_path = tokens[index]
            index += 1
            new_path: str | None = None
            if status[:1] in {"R", "C"}:
                if index >= len(tokens):
                    raise RuntimeError("malformed git rename/copy output")
                new_path = tokens[index]
                index += 1
            entries.append((status, old_path, new_path))
        self.metrics.diff_entries += len(entries)
        return entries


class CratesIndexHarvester:
    """Exhaustive and incremental crates.io discovery using the official Cargo Git index."""

    def __init__(self, *, queue: Any, git_client: Any | None = None):
        self.queue = queue
        repo_path = Path(queue.path).parent / "crates_io_index"
        self.git = git_client or GitIndexClient(repo_path)

    def _bootstrap_state(self) -> dict[str, Any]:
        return _cursor_json(self.queue.cursor(CRATES_BOOTSTRAP_SOURCE).get("cursor"))

    def _set_bootstrap_and_changes(self, state: dict[str, Any], commit: str) -> None:
        """Atomically mark bootstrap complete and activate the commit follower."""
        with self.queue.db:
            self.queue._set_cursor_no_commit(
                CRATES_BOOTSTRAP_SOURCE,
                cursor=_dump_cursor(state),
            )
            self.queue._set_cursor_no_commit(CRATES_CHANGES_SOURCE, cursor=commit)

    def bootstrap(self, *, limit: int = 500, reset: bool = False) -> dict[str, Any]:
        if reset:
            self.queue.reset_cursor(CRATES_BOOTSTRAP_SOURCE)
            self.queue.reset_cursor(CRATES_CHANGES_SOURCE)

        state = self._bootstrap_state()
        if state.get("complete"):
            if not self.git.exists:
                raise RuntimeError(
                    "crates.io bootstrap is complete but the local index clone is missing; "
                    "restore it or rerun with --reset"
                )
            commit = str(state.get("snapshot_commit") or "")
            if not str(self.queue.cursor(CRATES_CHANGES_SOURCE).get("cursor") or "").strip():
                self._set_bootstrap_and_changes(state, commit)
            return {
                "source": CRATES_BOOTSTRAP_SOURCE,
                "registry": CRATES_REGISTRY,
                "full": True,
                "complete": True,
                "snapshot_commit": commit,
                "changes_cursor": str(self.queue.cursor(CRATES_CHANGES_SOURCE).get("cursor") or commit),
                "discovered": 0,
                "inserted": 0,
                "updated": 0,
                "processed_paths": 0,
                "git": self.git.metrics.as_dict(),
            }

        if not state:
            if self.git.exists:
                self.git.fetch()
            else:
                self.git.ensure_clone()
            snapshot_commit = self.git.remote_head()
            state = {
                "complete": False,
                "snapshot_commit": snapshot_commit,
                "after_path": None,
                "seen_token": f"crates-bootstrap:{snapshot_commit}",
            }
            self.queue.set_cursor(CRATES_BOOTSTRAP_SOURCE, cursor=_dump_cursor(state))
        else:
            if not self.git.exists:
                raise RuntimeError(
                    "crates.io bootstrap cursor exists but the local index clone is missing; "
                    "restore it or rerun with --reset"
                )

        snapshot_commit = str(state.get("snapshot_commit") or "").strip()
        if not snapshot_commit:
            raise RuntimeError("crates.io bootstrap cursor is missing snapshot_commit")
        after_path = str(state.get("after_path") or "").strip() or None
        seen_token = str(state.get("seen_token") or f"crates-bootstrap:{snapshot_commit}")
        target = None if int(limit) <= 0 else max(1, int(limit))

        discovered = 0
        inserted = 0
        updated = 0
        processed_paths = 0
        batch: list[Any] = []
        batch_last_path: str | None = after_path
        exhausted = True

        # Local import avoids a module import cycle with technology_harvester.
        from .technology_harvester import HarvestCandidate

        for path in self.git.iter_paths(snapshot_commit):
            if after_path and path <= after_path:
                continue
            name = crate_name_from_index_path(path)
            if not name:
                continue
            processed_paths += 1
            batch_last_path = path
            batch.append(
                HarvestCandidate(
                    CRATES_REGISTRY,
                    name,
                    CRATES_BOOTSTRAP_SOURCE,
                    20,
                    seen_token=seen_token,
                )
            )
            discovered += 1

            reached_target = target is not None and discovered >= target
            if len(batch) >= _BATCH_SIZE or reached_target:
                next_state = {
                    **state,
                    "complete": False,
                    "after_path": batch_last_path,
                }
                page_inserted, page_updated = self.queue.upsert_many_with_cursor(
                    batch,
                    source=CRATES_BOOTSTRAP_SOURCE,
                    cursor=_dump_cursor(next_state),
                )
                inserted += page_inserted
                updated += page_updated
                state = next_state
                batch = []
                if reached_target:
                    exhausted = False
                    break

        if batch:
            next_state = {
                **state,
                "complete": False,
                "after_path": batch_last_path,
            }
            page_inserted, page_updated = self.queue.upsert_many_with_cursor(
                batch,
                source=CRATES_BOOTSTRAP_SOURCE,
                cursor=_dump_cursor(next_state),
            )
            inserted += page_inserted
            updated += page_updated
            state = next_state

        if exhausted:
            state = {
                **state,
                "complete": True,
                "after_path": batch_last_path or after_path,
            }
            self._set_bootstrap_and_changes(state, snapshot_commit)

        return {
            "source": CRATES_BOOTSTRAP_SOURCE,
            "registry": CRATES_REGISTRY,
            "full": True,
            "complete": bool(state.get("complete")),
            "snapshot_commit": snapshot_commit,
            "after_path": state.get("after_path"),
            "changes_cursor": str(self.queue.cursor(CRATES_CHANGES_SOURCE).get("cursor") or "") or None,
            "processed_paths": processed_paths,
            "discovered": discovered,
            "inserted": inserted,
            "updated": updated,
            "git": self.git.metrics.as_dict(),
        }

    @staticmethod
    def _events_from_diff(entries: Iterable[tuple[str, str, str | None]]) -> tuple[list[dict[str, Any]], int]:
        events: list[dict[str, Any]] = []
        ignored = 0
        for status, old_path, new_path in entries:
            kind = status[:1]
            if kind in {"R", "C"}:
                old_name = crate_name_from_index_path(old_path)
                new_name = crate_name_from_index_path(new_path or "")
                if kind == "R" and old_name and old_name != new_name:
                    events.append({"name": old_name, "deleted": True})
                if new_name:
                    events.append({"name": new_name, "deleted": False})
                elif not old_name:
                    ignored += 1
                continue

            name = crate_name_from_index_path(old_path)
            if not name:
                ignored += 1
                continue
            if kind == "D":
                events.append({"name": name, "deleted": True})
            elif kind in {"A", "M", "T"}:
                # M includes new versions and yanked/un-yanked version changes.
                events.append({"name": name, "deleted": False})
            else:
                ignored += 1
        return events, ignored

    def changes(self, *, limit: int = 500, reset: bool = False) -> dict[str, Any]:
        bootstrap = self._bootstrap_state()
        if reset:
            self.queue.reset_cursor(CRATES_CHANGES_SOURCE)

        if not bootstrap.get("complete"):
            return {
                "source": CRATES_CHANGES_SOURCE,
                "registry": CRATES_REGISTRY,
                "full": False,
                "bootstrap_required": True,
                "complete_bootstrap": False,
                "discovered": 0,
                "inserted": 0,
                "updated": 0,
                "deleted": 0,
                "git": self.git.metrics.as_dict(),
            }

        snapshot_commit = str(bootstrap.get("snapshot_commit") or "").strip()
        state = self.queue.cursor(CRATES_CHANGES_SOURCE)
        previous_commit = str(state.get("cursor") or snapshot_commit).strip()
        if not previous_commit:
            previous_commit = snapshot_commit
            self.queue.set_cursor(CRATES_CHANGES_SOURCE, cursor=previous_commit)

        if not self.git.exists:
            raise RuntimeError(
                "crates.io incremental cursor exists but the local index clone is missing; "
                "restore it before following changes"
            )

        self.git.fetch()
        current_commit = self.git.remote_head()
        if current_commit == previous_commit:
            return {
                "source": CRATES_CHANGES_SOURCE,
                "registry": CRATES_REGISTRY,
                "full": False,
                "bootstrap_required": False,
                "previous_cursor": previous_commit,
                "cursor": current_commit,
                "changed_paths": 0,
                "events": 0,
                "discovered": 0,
                "inserted": 0,
                "updated": 0,
                "deleted": 0,
                "git": self.git.metrics.as_dict(),
            }

        entries = self.git.diff_entries(previous_commit, current_commit)
        events, ignored = self._events_from_diff(entries)
        counts = self.queue.apply_change_events(
            registry=CRATES_REGISTRY,
            source=CRATES_CHANGES_SOURCE,
            events=events,
            cursor=current_commit,
            priority=65,
        )
        discovered = sum(1 for event in events if not event.get("deleted"))
        requested_limit = int(limit)
        return {
            "source": CRATES_CHANGES_SOURCE,
            "registry": CRATES_REGISTRY,
            "full": False,
            "bootstrap_required": False,
            "previous_cursor": previous_commit,
            "cursor": current_commit,
            "changed_paths": len(entries),
            "events": len(events),
            "unique_packages": len({str(event.get("name")) for event in events if event.get("name")}),
            "ignored_paths": ignored,
            "discovered": discovered,
            "inserted": int(counts["inserted"]),
            "updated": int(counts["updated"]),
            "deleted": int(counts["deleted"]),
            "limit_ignored_for_cursor_safety": bool(requested_limit > 0 and len(entries) > requested_limit),
            "git": self.git.metrics.as_dict(),
        }

    def harvest(self, *, full: bool = False, limit: int = 500, reset: bool = False) -> dict[str, Any]:
        return self.bootstrap(limit=limit, reset=reset) if full else self.changes(limit=limit, reset=reset)
