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


class _Task:
    task_type = "fix_bug"
    description = "change code"


class _Cfg:
    max_changed_files_per_pr = 3
    max_lines_changed_per_pr = 200
    allowed_modification_paths = ("",)
    forbidden_modification_paths = ()


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


class TestResetWorktree:
    def test_reset_restores_pre_existing_dirty_files(self, repo):
        (repo / "learnings.md").write_text("entry one\nlocal note\n")
        dirty_before = backends._snapshot_tracked_dirty(repo)
        untracked_before = backends._untracked_files(repo)

        (repo / "learnings.md").write_text("agent overwrote local note\n")
        (repo / "code.py").write_text("agent changed code\n")

        backends._reset_worktree(repo, untracked_before, dirty_before)

        assert (repo / "learnings.md").read_text() == "entry one\nlocal note\n"
        assert (repo / "code.py").read_text() == "original\n"

    def test_deleted_tracked_file_is_collected_then_restored_with_dirty_file(self, repo):
        (repo / "code.py").write_text("local edit\n")
        dirty_before = backends._snapshot_tracked_dirty(repo)
        untracked_before = backends._untracked_files(repo)

        (repo / "learnings.md").unlink()
        (repo / "code.py").write_text("local edit + agent edit\n")

        changes = backends._collect_changes(repo, untracked_before, _Change, dirty_before)
        by_path = {change.file_path: change for change in changes}
        assert set(by_path) == {"code.py", "learnings.md"}
        assert by_path["learnings.md"].original_content == "entry one\n"
        assert by_path["learnings.md"].new_content == ""
        assert by_path["code.py"].new_content == "local edit + agent edit\n"

        backends._reset_worktree(repo, untracked_before, dirty_before)

        assert (repo / "learnings.md").read_text() == "entry one\n"
        assert (repo / "code.py").read_text() == "local edit\n"

    def test_reset_removes_agent_created_untracked_files(self, repo):
        (repo / "local.db").write_text("keep\n")
        untracked_before = backends._untracked_files(repo)
        dirty_before = backends._snapshot_tracked_dirty(repo)
        (repo / "new_file.py").write_text("remove\n")

        backends._reset_worktree(repo, untracked_before, dirty_before)

        assert (repo / "local.db").read_text() == "keep\n"
        assert not (repo / "new_file.py").exists()

    def test_reset_handles_staged_changes(self, repo):
        (repo / "learnings.md").write_text("entry one\nlocal note\n")
        dirty_before = backends._snapshot_tracked_dirty(repo)
        untracked_before = backends._untracked_files(repo)
        (repo / "code.py").write_text("agent changed code\n")
        _git(repo, "add", "code.py")

        backends._reset_worktree(repo, untracked_before, dirty_before)

        assert (repo / "learnings.md").read_text() == "entry one\nlocal note\n"
        assert (repo / "code.py").read_text() == "original\n"
        assert _git(repo, "diff", "--cached", "--name-only").stdout == ""

    def test_reset_with_empty_or_none_dirty_before(self, repo):
        untracked_before = backends._untracked_files(repo)
        (repo / "code.py").write_text("agent changed code\n")
        backends._reset_worktree(repo, untracked_before, None)
        assert (repo / "code.py").read_text() == "original\n"

        (repo / "code.py").write_text("agent changed code again\n")
        backends._reset_worktree(repo, untracked_before, {})
        assert (repo / "code.py").read_text() == "original\n"

    @pytest.mark.parametrize("crash", [False, True])
    def test_agent_generate_changes_lifecycle_preserves_dirty_files(
        self, monkeypatch, repo, crash
    ):
        (repo / "learnings.md").write_text("entry one\nlocal note\n")

        def fake_run_claude(binary, prompt, *, model=None, cwd=None, edit=False, timeout=600):
            (repo / "code.py").write_text("agent changed code\n")
            if crash:
                raise RuntimeError("agent died")
            return "done", {"prompt_tokens": 2, "completion_tokens": 1}

        monkeypatch.setattr(backends, "resolve_binary", lambda name: "/usr/bin/claude")
        monkeypatch.setattr(backends, "_run_claude", fake_run_claude)

        changes, usage = backends.agent_generate_changes(
            _Task(), "plan", repo, _Cfg(), "claude",
        )

        assert (repo / "learnings.md").read_text() == "entry one\nlocal note\n"
        assert (repo / "code.py").read_text() == "original\n"
        if crash:
            assert changes is None
            assert usage is None
        else:
            assert [change.file_path for change in changes] == ["code.py"]
            assert usage == {"prompt_tokens": 2, "completion_tokens": 1}


class TestRuntimeStateIsolation:
    """#139: a runtime state file that changed during the agent run was
    collected as an agent edit, and `config/state.json` then failed the
    forbidden-path policy before any code was produced."""

    @pytest.fixture
    def state_repo(self, repo):
        (repo / "config").mkdir()
        (repo / "config" / "state.json").write_text('{"cycle": 1}\n')
        (repo / "docs" / "wiki").mkdir(parents=True)
        (repo / "docs" / "wiki" / "Home.md").write_text("home\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "state")
        return repo

    def test_state_and_source_edits_in_one_run(self, monkeypatch, state_repo):
        repo = state_repo
        state = repo / "config" / "state.json"
        state.write_text('{"cycle": 2}\n')             # dirty BEFORE the run

        def fake_run_claude(binary, prompt, *, model=None, cwd=None, edit=False, timeout=600):
            state.write_text('{"cycle": 3}\n')         # state rewritten mid-run
            (repo / "config" / "metrics.json").write_text("{}\n")   # state created mid-run
            (repo / "code.py").write_text("agent changed code\n")
            (repo / "docs" / "wiki" / "Home.md").write_text("agent wiki edit\n")
            return "done", {"prompt_tokens": 2, "completion_tokens": 1}

        monkeypatch.setattr(backends, "resolve_binary", lambda name: "/usr/bin/claude")
        monkeypatch.setattr(backends, "_run_claude", fake_run_claude)

        changes, _usage = backends.agent_generate_changes(
            _Task(), "plan", repo, _Cfg(), "claude",
        )

        assert sorted(c.file_path for c in changes) == ["code.py", "docs/wiki/Home.md"]
        # The pre-existing state change survives; the mid-run ones are gone.
        assert state.read_text() == '{"cycle": 2}\n'
        assert not (repo / "config" / "metrics.json").exists()
        assert (repo / "code.py").read_text() == "original\n"
