"""Tests for git_ops module."""

import time
from unittest.mock import patch, MagicMock

import pytest
from pathlib import Path

from ouroboros.git_ops import (
    _decode_git_path,
    _is_auto_state_path,
    _git_porcelain_changes,
    _git_porcelain_target_path,
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


@patch("ouroboros.git_ops.current_branch", return_value="main")
@patch("ouroboros.git_ops._git")
def test_commit_auto_state_handles_renamed_state_paths(mock_git, mock_branch):
    rel_path = 'docs/wiki/new -> state "note" \u00e9.md'
    porcelain_path = r'"docs/wiki/old -> state.md" -> "docs/wiki/new -> state \"note\" \303\251.md"'
    mock_git.side_effect = [
        MagicMock(stdout=f"R  {porcelain_path}\n"),
        MagicMock(),  # git add
        MagicMock(stdout=f"{rel_path}\n"),  # diff --cached
        MagicMock(),  # git commit
        MagicMock(),  # git push
    ]

    assert commit_auto_state(Path("/tmp/repo")) is True
    mock_git.assert_any_call(Path("/tmp/repo"), "add", rel_path)


@patch("ouroboros.git_ops.current_branch", return_value="main")
@patch("ouroboros.git_ops._git")
def test_commit_auto_state_handles_copied_state_paths(mock_git, mock_branch):
    rel_path = "docs/wiki/copied -> state.md"
    mock_git.side_effect = [
        MagicMock(stdout='C  "docs/wiki/original -> state.md" -> "docs/wiki/copied -> state.md"\n'),
        MagicMock(),  # git add
        MagicMock(stdout=f"{rel_path}\n"),  # diff --cached
        MagicMock(),  # git commit
        MagicMock(),  # git push
    ]

    assert commit_auto_state(Path("/tmp/repo")) is True
    mock_git.assert_any_call(Path("/tmp/repo"), "add", rel_path)


@patch("ouroboros.git_ops.current_branch", return_value="main")
@patch("ouroboros.git_ops._git")
def test_commit_auto_state_preserves_arrow_in_modified_path(mock_git, mock_branch):
    rel_path = "docs/wiki/state -> note.md"
    mock_git.side_effect = [
        MagicMock(stdout=' M "docs/wiki/state -> note.md"\n'),
        MagicMock(),  # git add
        MagicMock(stdout=f"{rel_path}\n"),  # diff --cached
        MagicMock(),  # git commit
        MagicMock(),  # git push
    ]

    assert commit_auto_state(Path("/tmp/repo")) is True
    mock_git.assert_any_call(Path("/tmp/repo"), "add", rel_path)


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


# -- C-quoted path decoding (migrated here from backends) --------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("config/state.json", "config/state.json"),          # unquoted, unchanged
        ('"config/state.json"', "config/state.json"),         # quoted, no escapes
        ('"config/state with space.json"', "config/state with space.json"),
        (r'"config/\303\274mlaut.json"', "config/ümlaut.json"),  # octal UTF-8
        (r'"config/say \"hi\".json"', 'config/say "hi".json'),   # escaped quotes
        (r'"config/back\\slash.json"', "config/back\\slash.json"),
        (r'"config/tab\there.json"', "config/tab\there.json"),
        ('"', '"'),                                           # too short to be quoted
    ],
)
def test_decode_git_path(raw, expected):
    assert _decode_git_path(raw) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (" M config/state.json", "config/state.json"),
        (' M "config/state with space.json"', "config/state with space.json"),
        ("?? config/new.json", "config/new.json"),
        ('R  old.json -> new.json', "new.json"),
        # " -> " inside a quoted filename must not be treated as the separator
        ('R  "a.json" -> "weird -> name.json"', "weird -> name.json"),
        (' M "has -> arrow.json"', "has -> arrow.json"),
    ],
)
def test_git_porcelain_target_path(line, expected):
    assert _git_porcelain_target_path(line) == expected


def test_git_porcelain_changes_skips_blank_lines():
    porcelain = ' M config/state.json\n\n?? config/new.json\n'
    assert list(_git_porcelain_changes(porcelain)) == [
        (" M", "config/state.json"),
        ("??", "config/new.json"),
    ]


@patch("ouroboros.git_ops.current_branch", return_value="main")
@patch("ouroboros.git_ops._git")
def test_commit_auto_state_stages_quoted_state_paths(mock_git, mock_branch):
    """Quoted paths must be decoded before the prefix check and the git add.

    Without decoding, the path still carries its surrounding quotes, fails
    startswith("config/"), and the state file is silently never committed.
    """
    mock_git.side_effect = [
        MagicMock(stdout=' M "docs/wiki/page with space.md"\n M config/metrics.json\n'),
        MagicMock(),                                          # git add
        MagicMock(stdout="docs/wiki/page with space.md\n"),   # diff --cached
        MagicMock(),                                          # git commit
        MagicMock(),                                          # git push
    ]

    assert commit_auto_state(Path("/tmp/repo")) is True

    add_call = mock_git.call_args_list[1]
    assert add_call.args[1] == "add"
    assert add_call.args[2:] == ("docs/wiki/page with space.md", "config/metrics.json")


@patch("ouroboros.git_ops.current_branch", return_value="main")
@patch("ouroboros.git_ops._git")
def test_commit_auto_state_decodes_octal_escaped_state_paths(mock_git, mock_branch):
    mock_git.side_effect = [
        MagicMock(stdout=' M "docs/wiki/\\303\\274mlaut.md"\n'),
        MagicMock(),
        MagicMock(stdout="docs/wiki/ümlaut.md\n"),
        MagicMock(),
        MagicMock(),
    ]

    assert commit_auto_state(Path("/tmp/repo")) is True
    assert mock_git.call_args_list[1].args[2:] == ("docs/wiki/ümlaut.md",)


def test_commit_auto_state_is_defined_in_git_ops():
    """Regression guard: this logic belongs in git_ops, not a monkeypatch.

    It previously lived in ouroboros/__init__.py, which rebound
    git_ops.commit_auto_state at package-import time so the source here was
    dead code.
    """
    import inspect

    from ouroboros import backends, git_ops  # noqa: F401  (import triggers any patching)

    assert inspect.getsourcefile(git_ops.commit_auto_state) == git_ops.__file__


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("config/state.json", True),
        ("config/metrics.json", True),
        ("docs/wiki", True),                  # the directory itself
        ("docs/wiki/architecture.md", True),  # anything under it
        ("docs/wiki/nested/deep.md", True),
        # Exact entries must not match by prefix -- commit_auto_state pushes
        # whatever it stages.
        ("config/state.json.backup", False),
        ("config/state.json.orig", False),
        ("config/metrics.json.tmp", False),
        ("config/other.json", False),
        ("docs/wikipedia/page.md", False),
        ("src/ouroboros/git_ops.py", False),
    ],
)
def test_is_auto_state_path(path, expected):
    assert _is_auto_state_path(path) is expected


@patch("ouroboros.git_ops.current_branch", return_value="main")
@patch("ouroboros.git_ops._git")
def test_commit_auto_state_ignores_sibling_of_state_file(mock_git, mock_branch):
    """config/state.json.backup must not be swept in by a prefix match."""
    mock_git.side_effect = [
        MagicMock(stdout=" M config/state.json.backup\n"),
    ]

    assert commit_auto_state(Path("/tmp/repo")) is False
    assert mock_git.call_count == 1  # never reached git add
