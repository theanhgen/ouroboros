"""Tests for git_ops module."""

import time
from unittest.mock import patch, MagicMock
from pathlib import Path

from ouroboros.git_ops import (
    _safe_git_env,
    make_branch_name,
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
