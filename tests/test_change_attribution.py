"""The agent's change set must contain only what the agent changed.

`_collect_changes` compares the worktree against HEAD, so any file that was
already dirty when the cycle started looked exactly like an agent edit. On
2026-08-21 that swept an untouched `config/learnings.md` into the change set and
tripped the forbidden-path policy -- failing a cycle whose real code changes
were fine, and blaming the model for an edit it never made.
"""

import subprocess

import pytest

from ouroboros import backends


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "code.py").write_text("original\n")
    (tmp_path / "learnings.md").write_text("entry one\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


class _Change:
    def __init__(self, file_path, original_content, new_content, description):
        self.file_path = file_path
        self.original_content = original_content
        self.new_content = new_content
        self.description = description


class TestSnapshot:
    def test_captures_modified_tracked_files(self, repo):
        (repo / "code.py").write_text("locally edited\n")
        snap = backends._snapshot_tracked_dirty(repo)
        assert snap == {"code.py": "locally edited\n"}

    def test_a_clean_tree_snapshots_nothing(self, repo):
        assert backends._snapshot_tracked_dirty(repo) == {}

    def test_untracked_files_are_not_snapshotted(self, repo):
        (repo / "new.py").write_text("x\n")
        assert backends._snapshot_tracked_dirty(repo) == {}


class TestCollectChanges:
    def test_an_agent_edit_is_collected(self, repo):
        before = backends._snapshot_tracked_dirty(repo)
        (repo / "code.py").write_text("agent wrote this\n")
        changes = backends._collect_changes(repo, set(), _Change, before)
        assert [c.file_path for c in changes] == ["code.py"]
        assert changes[0].new_content == "agent wrote this\n"

    def test_a_pre_existing_dirty_file_is_not_blamed_on_the_agent(self, repo):
        """The 2026-08-21 regression, exactly."""
        (repo / "learnings.md").write_text("entry one\nentry two\n")   # dirty BEFORE
        before = backends._snapshot_tracked_dirty(repo)
        (repo / "code.py").write_text("agent wrote this\n")            # the agent's real edit

        changes = backends._collect_changes(repo, set(), _Change, before)
        paths = [c.file_path for c in changes]
        assert "learnings.md" not in paths, "an untouched dirty file was attributed to the agent"
        assert paths == ["code.py"]

    def test_the_agent_editing_an_already_dirty_file_is_still_collected(self, repo):
        """The exclusion must be content-based, not path-based -- otherwise a
        genuine agent edit to a dirty file would be silently dropped."""
        (repo / "code.py").write_text("local edit\n")
        before = backends._snapshot_tracked_dirty(repo)
        (repo / "code.py").write_text("local edit + agent edit\n")

        changes = backends._collect_changes(repo, set(), _Change, before)
        assert [c.file_path for c in changes] == ["code.py"]
        assert changes[0].new_content == "local edit + agent edit\n"

    def test_without_the_snapshot_the_old_behaviour_returns(self, repo):
        """Guards the guard: omitting dirty_before reproduces the bug, which is
        why the caller must pass it."""
        (repo / "learnings.md").write_text("entry one\nentry two\n")
        changes = backends._collect_changes(repo, set(), _Change)
        assert [c.file_path for c in changes] == ["learnings.md"]

    def test_pre_existing_untracked_files_are_still_ignored(self, repo):
        (repo / "local.db").write_text("junk\n")
        untracked_before = backends._untracked_files(repo)
        before = backends._snapshot_tracked_dirty(repo)
        (repo / "code.py").write_text("agent\n")

        changes = backends._collect_changes(repo, untracked_before, _Change, before)
        assert [c.file_path for c in changes] == ["code.py"]

    def test_an_agent_created_file_is_collected(self, repo):
        untracked_before = backends._untracked_files(repo)
        before = backends._snapshot_tracked_dirty(repo)
        (repo / "brand_new.py").write_text("created by agent\n")

        changes = backends._collect_changes(repo, untracked_before, _Change, before)
        assert [c.file_path for c in changes] == ["brand_new.py"]
        assert changes[0].original_content == ""
