"""Tests for git_ops module."""

import time
from unittest.mock import patch, MagicMock
from pathlib import Path

from ouroboros.git_ops import (
    _safe_git_env,
    commit_auto_state,
    create_issue,
    find_open_issue_by_marker,
    make_branch_name,
    make_auto_issue_marker,
    is_clean,
    current_branch,
)


def test_safe_git_env():
    env = _safe_git_env()
    assert env["GIT_AUTHOR_NAME"] == "ouroboros-bot"
    assert env["GIT_AUTHOR_EMAIL"] == "ouroboros-bot@localhost"
    assert env["GIT_COMMITTER_NAME"] == "ouroboros-bot"
    assert env["GIT_COMMITTER_EMAIL"] == "ouroboros-bot@localhost"


def test_safe_git_env_preserves_existing():
    with patch.dict("os.environ", {"GIT_AUTHOR_NAME": "custom"}):
        env = _safe_git_env()
        assert env["GIT_AUTHOR_NAME"] == "custom"


def test_make_branch_name():
    name = make_branch_name("fix_test")
    assert name.startswith("ouroboros/improve-fix_test-")
    # Should contain a timestamp
    parts = name.split("-")
    assert len(parts) >= 3


def test_make_branch_name_types():
    for task_type in ["fix_test", "add_test", "fix_bug"]:
        name = make_branch_name(task_type)
        assert task_type in name


def test_make_auto_issue_marker_is_stable():
    marker_a = make_auto_issue_marker("fix_bug", "Tighten retry handling", ["src/a.py", "tests/test_a.py"])
    marker_b = make_auto_issue_marker("fix_bug", "Tighten retry handling", ["src/a.py", "tests/test_a.py"])
    marker_c = make_auto_issue_marker("fix_bug", "Different task", ["src/a.py", "tests/test_a.py"])

    assert marker_a == marker_b
    assert marker_a.startswith("<!-- ouroboros:auto-issue:")
    assert marker_a != marker_c


@patch("ouroboros.git_ops._git")
def test_is_clean(mock_git):
    mock_git.return_value = MagicMock(stdout="")
    assert is_clean(Path("/tmp/repo")) is True


@patch("ouroboros.git_ops._git")
def test_is_not_clean(mock_git):
    mock_git.return_value = MagicMock(stdout=" M src/ouroboros/config.py")
    assert is_clean(Path("/tmp/repo")) is False


@patch("ouroboros.git_ops._git")
def test_current_branch(mock_git):
    mock_git.return_value = MagicMock(stdout="main\n")
    assert current_branch(Path("/tmp/repo")) == "main"


@patch("ouroboros.git_ops._git")
def test_commit_auto_state_nothing_dirty(mock_git):
    mock_git.return_value = MagicMock(stdout="")
    assert commit_auto_state(Path("/tmp/repo")) is False


@patch("ouroboros.git_ops.current_branch", return_value="main")
@patch("ouroboros.git_ops._git")
def test_commit_auto_state_commits_state_files(mock_git, mock_branch):
    # First call: status --porcelain returns dirty state files
    # Second call: git add
    # Third call: diff --cached returns staged files
    # Fourth call: git commit
    # Fifth call: git push
    mock_git.side_effect = [
        MagicMock(stdout=" M config/state.json\n M config/improvement_history.json\n"),
        MagicMock(),  # git add
        MagicMock(stdout="config/state.json\nconfig/improvement_history.json\n"),  # diff --cached
        MagicMock(),  # git commit
        MagicMock(),  # git push
    ]
    assert commit_auto_state(Path("/tmp/repo")) is True


@patch("ouroboros.git_ops._git")
def test_commit_auto_state_ignores_non_state_files(mock_git):
    # Only non-state files are dirty -- should not commit
    mock_git.return_value = MagicMock(stdout=" M src/ouroboros/config.py\n")
    assert commit_auto_state(Path("/tmp/repo")) is False


@patch("subprocess.run")
def test_create_issue(mock_run):
    mock_run.return_value = MagicMock(stdout="https://github.com/repo/issues/7\n")
    url = create_issue(Path("/tmp/repo"), "title", "body")
    assert url == "https://github.com/repo/issues/7"


@patch("subprocess.run")
def test_find_open_issue_by_marker(mock_run):
    mock_run.return_value = MagicMock(
        stdout='[{"body":"hello <!-- ouroboros:auto-issue:abc -->","url":"https://github.com/repo/issues/8"}]'
    )
    url = find_open_issue_by_marker(Path("/tmp/repo"), "<!-- ouroboros:auto-issue:abc -->")
    assert url == "https://github.com/repo/issues/8"
