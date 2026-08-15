from __future__ import annotations

from pathlib import Path

from ivoiredata.technology_crates import (
    CRATES_BOOTSTRAP_SOURCE,
    CRATES_CHANGES_SOURCE,
    CratesIndexHarvester,
    GitMetrics,
    crate_name_from_index_path,
)
from ivoiredata.technology_harvester import HarvestCandidate, TechnologyHarvestQueue


class FakeGitIndex:
    def __init__(self, *, head="aaa", paths=None, diffs=None, exists=True):
        self.head = head
        self.paths = paths or {}
        self.diffs = diffs or {}
        self._exists = exists
        self.metrics = GitMetrics()
        self.fetch_calls = 0
        self.clone_calls = 0
        self.remote_head_calls = 0

    @property
    def exists(self):
        return self._exists

    def ensure_clone(self):
        self.clone_calls += 1
        self.metrics.commands += 1
        self.metrics.network_operations += 1
        self.metrics.clone_performed = True
        self._exists = True

    def fetch(self):
        self.fetch_calls += 1
        self.metrics.commands += 1
        self.metrics.network_operations += 1
        self.metrics.fetch_performed = True

    def remote_head(self):
        self.remote_head_calls += 1
        self.metrics.commands += 1
        return self.head

    def iter_paths(self, commit):
        self.metrics.commands += 1
        for path in self.paths.get(commit, []):
            self.metrics.paths_scanned += 1
            yield path

    def diff_entries(self, old_commit, new_commit):
        self.metrics.commands += 1
        entries = list(self.diffs.get((old_commit, new_commit), []))
        self.metrics.diff_entries += len(entries)
        return entries


def _candidate_status(queue, name):
    row = queue.db.execute(
        "SELECT status FROM candidates WHERE registry='crates.io' AND name=?", (name,)
    ).fetchone()
    return row["status"] if row else None


def test_crates_index_path_rules_match_cargo_layout():
    assert crate_name_from_index_path("1/a") == "a"
    assert crate_name_from_index_path("2/ab") == "ab"
    assert crate_name_from_index_path("3/f/foo") == "foo"
    assert crate_name_from_index_path("se/rd/serde") == "serde"
    assert crate_name_from_index_path("config.json") is None
    assert crate_name_from_index_path(".github/workflows/ci.yml") is None
    assert crate_name_from_index_path("README.md") is None
    assert crate_name_from_index_path("se/rd/not/a/crate") is None


def test_changes_refuses_false_global_coverage_before_bootstrap(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "technology_harvest.sqlite3")
    git = FakeGitIndex(head="aaa", paths={"aaa": ["se/rd/serde"]})
    try:
        result = CratesIndexHarvester(queue=queue, git_client=git).changes(limit=10)
        assert result["bootstrap_required"] is True
        assert result["discovered"] == 0
        assert git.fetch_calls == 0
        assert queue.cursor(CRATES_CHANGES_SOURCE) == {}
    finally:
        queue.close()


def test_bounded_bootstrap_resumes_same_snapshot_then_activates_follower(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "technology_harvest.sqlite3")
    git = FakeGitIndex(
        head="aaa",
        paths={
            "aaa": [
                "config.json",
                "1/a",
                "3/b/bar",
                "se/rd/serde",
            ]
        },
    )
    try:
        harvester = CratesIndexHarvester(queue=queue, git_client=git)
        first = harvester.bootstrap(limit=2)
        assert first["complete"] is False
        assert first["snapshot_commit"] == "aaa"
        assert first["discovered"] == 2
        assert first["after_path"] == "3/b/bar"
        assert queue.cursor(CRATES_CHANGES_SOURCE) == {}
        assert git.fetch_calls == 1

        # The upstream head may advance while the bootstrap is running. Resume must stay
        # pinned to the original snapshot and must not fetch a new tree mid-bootstrap.
        git.head = "bbb"
        second = harvester.bootstrap(limit=2)
        assert second["complete"] is True
        assert second["snapshot_commit"] == "aaa"
        assert second["discovered"] == 1
        assert second["changes_cursor"] == "aaa"
        assert git.fetch_calls == 1
        assert queue.audit()["by_registry"]["crates.io"] == 3
    finally:
        queue.close()


def test_completed_bootstrap_rerun_is_zero_git_work(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "technology_harvest.sqlite3")
    first_git = FakeGitIndex(head="aaa", paths={"aaa": ["1/a"]})
    try:
        first = CratesIndexHarvester(queue=queue, git_client=first_git).bootstrap(limit=0)
        assert first["complete"] is True

        second_git = FakeGitIndex(head="bbb", paths={"bbb": ["1/a", "1/b"]})
        second = CratesIndexHarvester(queue=queue, git_client=second_git).bootstrap(limit=500)
        assert second["complete"] is True
        assert second["discovered"] == 0
        assert second["git"]["commands"] == 0
        assert second["git"]["network_operations"] == 0
        assert second_git.fetch_calls == 0
        assert second_git.remote_head_calls == 0
    finally:
        queue.close()


def test_git_diff_requeues_updates_yanks_and_marks_deletions(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "technology_harvest.sqlite3")
    bootstrap_git = FakeGitIndex(
        head="aaa",
        paths={"aaa": ["ol/d_/old", "se/rd/serde"]},
    )
    try:
        CratesIndexHarvester(queue=queue, git_client=bootstrap_git).bootstrap(limit=0)
        queue.mark_qualified("crates.io", "serde")
        queue.mark_qualified("crates.io", "old")

        delta_git = FakeGitIndex(
            head="bbb",
            diffs={
                ("aaa", "bbb"): [
                    ("M", "se/rd/serde", None),  # new version or yanked change
                    ("D", "ol/d_/old", None),
                    ("A", "to/ki/tokio", None),
                    ("M", "config.json", None),
                ]
            },
        )
        result = CratesIndexHarvester(queue=queue, git_client=delta_git).changes(limit=1)
        assert result["bootstrap_required"] is False
        assert result["previous_cursor"] == "aaa"
        assert result["cursor"] == "bbb"
        assert result["changed_paths"] == 4
        assert result["events"] == 3
        assert result["ignored_paths"] == 1
        assert result["inserted"] == 1
        assert result["updated"] == 1
        assert result["deleted"] == 1
        assert result["limit_ignored_for_cursor_safety"] is True
        assert _candidate_status(queue, "serde") == "PENDING"
        assert _candidate_status(queue, "old") == "DELETED"
        assert _candidate_status(queue, "tokio") == "PENDING"
        assert queue.cursor(CRATES_CHANGES_SOURCE)["cursor"] == "bbb"
    finally:
        queue.close()


def test_same_git_head_has_no_diff_and_no_reprocessing(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "technology_harvest.sqlite3")
    bootstrap_git = FakeGitIndex(head="aaa", paths={"aaa": ["se/rd/serde"]})
    try:
        CratesIndexHarvester(queue=queue, git_client=bootstrap_git).bootstrap(limit=0)
        queue.mark_qualified("crates.io", "serde")

        delta_git = FakeGitIndex(head="aaa")
        result = CratesIndexHarvester(queue=queue, git_client=delta_git).changes(limit=500)
        assert result["changed_paths"] == 0
        assert result["events"] == 0
        assert result["discovered"] == 0
        assert delta_git.fetch_calls == 1
        assert delta_git.metrics.diff_entries == 0
        assert _candidate_status(queue, "serde") == "QUALIFIED"
    finally:
        queue.close()


def test_rename_is_tombstone_plus_new_candidate_when_name_changes(tmp_path: Path):
    queue = TechnologyHarvestQueue(tmp_path / "technology_harvest.sqlite3")
    bootstrap_git = FakeGitIndex(head="aaa", paths={"aaa": ["ol/d_/old"]})
    try:
        CratesIndexHarvester(queue=queue, git_client=bootstrap_git).bootstrap(limit=0)
        delta_git = FakeGitIndex(
            head="bbb",
            diffs={("aaa", "bbb"): [("R100", "ol/d_/old", "ne/w_/new")]},
        )
        result = CratesIndexHarvester(queue=queue, git_client=delta_git).changes()
        assert result["events"] == 2
        assert _candidate_status(queue, "old") == "DELETED"
        assert _candidate_status(queue, "new") == "PENDING"
    finally:
        queue.close()
