"""Tests for git_ops module."""

import json
import shutil
import subprocess
import time
from unittest.mock import patch, MagicMock

import pytest
from pathlib import Path

from ouroboros.git_ops import (
    _decode_git_path,
    auto_merge_pr,
    get_pr_checks_status,
    get_pr_feedback,
    get_pr_status,
    has_open_improvement_prs,
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


# -- has_open_improvement_prs ------------------------------------------------

def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


@patch("ouroboros.git_ops.subprocess.run")
def test_has_open_improvement_prs_true(mock_run):
    mock_run.return_value = _completed("ouroboros/improve-fix_bug-123\nfeature/other\n")
    assert has_open_improvement_prs(Path("/repo")) is True


@patch("ouroboros.git_ops.subprocess.run")
def test_has_open_improvement_prs_ignores_other_branches(mock_run):
    """Only the agent's own branches gate the next cycle."""
    mock_run.return_value = _completed("feature/x\nbugfix/y\nouroboros/other-thing\n")
    assert has_open_improvement_prs(Path("/repo")) is False


@patch("ouroboros.git_ops.subprocess.run")
def test_has_open_improvement_prs_no_open_prs(mock_run):
    mock_run.return_value = _completed("")
    assert has_open_improvement_prs(Path("/repo")) is False


@patch("ouroboros.git_ops.subprocess.run")
def test_has_open_improvement_prs_whitespace_only(mock_run):
    mock_run.return_value = _completed("\n  \n")
    assert has_open_improvement_prs(Path("/repo")) is False


@patch("ouroboros.git_ops.subprocess.run")
def test_has_open_improvement_prs_queries_open_only(mock_run):
    mock_run.return_value = _completed("")
    has_open_improvement_prs(Path("/repo"))

    cmd = mock_run.call_args.args[0]
    kwargs = mock_run.call_args.kwargs
    assert cmd[:3] == ["gh", "pr", "list"]
    assert "--state" in cmd and cmd[cmd.index("--state") + 1] == "open"
    # gh pr list defaults to 30; the guard has to see every open PR, not a page.
    assert "--limit" in cmd and int(cmd[cmd.index("--limit") + 1]) >= 100
    assert kwargs["cwd"] == Path("/repo")
    assert kwargs["check"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 30


@patch("ouroboros.git_ops.subprocess.run")
def test_has_open_improvement_prs_sees_beyond_the_first_page(mock_run):
    """A matching PR after the default 30 must still be found."""
    branches = [f"feature/pr-{i}" for i in range(40)]
    branches.append("ouroboros/improve-fix_bug-999")
    mock_run.return_value = _completed("\n".join(branches))

    assert has_open_improvement_prs(Path("/repo")) is True


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["gh"]),
        FileNotFoundError("gh not installed"),
        subprocess.TimeoutExpired(["gh"], 30),
    ],
)
@patch("ouroboros.git_ops.subprocess.run")
def test_has_open_improvement_prs_unknown_on_gh_failure(mock_run, error):
    """Unknown must not read as "none".

    This gates whether another cycle may start, and callers treat False as
    permission -- so a GitHub outage returning False would let the agent open
    a second PR for work already in flight.
    """
    mock_run.side_effect = error
    assert has_open_improvement_prs(Path("/repo")) is None


# -- get_pr_status -----------------------------------------------------------

@pytest.mark.parametrize("state", ["MERGED", "CLOSED", "OPEN"])
@patch("ouroboros.git_ops.subprocess.run")
def test_get_pr_status_returns_the_state(mock_run, state):
    mock_run.return_value = _completed(f"{state}\n")
    assert get_pr_status(Path("/repo"), "some-branch") == state


@patch("ouroboros.git_ops.subprocess.run")
def test_get_pr_status_passes_the_branch(mock_run):
    mock_run.return_value = _completed("OPEN")
    get_pr_status(Path("/repo"), "ouroboros/improve-fix_bug-1")

    cmd = mock_run.call_args.args[0]
    assert cmd[:3] == ["gh", "pr", "view"]
    assert "ouroboros/improve-fix_bug-1" in cmd


@patch("ouroboros.git_ops.subprocess.run")
def test_get_pr_status_empty_output(mock_run):
    mock_run.return_value = _completed("")
    assert get_pr_status(Path("/repo"), "branch") == ""


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["gh"]),   # no PR for this branch
        FileNotFoundError("gh not installed"),
        subprocess.TimeoutExpired(["gh"], 30),
    ],
)
@patch("ouroboros.git_ops.subprocess.run")
def test_get_pr_status_none_on_failure(mock_run, error):
    mock_run.side_effect = error
    assert get_pr_status(Path("/repo"), "branch") is None


# -- get_pr_feedback ---------------------------------------------------------

@patch("ouroboros.git_ops.subprocess.run")
def test_get_pr_feedback_returns_review_text(mock_run):
    mock_run.return_value = _completed("Looks good\n---\nOne nit\n")
    assert get_pr_feedback(Path("/repo"), "https://x/pr/1") == "Looks good\n---\nOne nit"


@patch("ouroboros.git_ops.subprocess.run")
def test_get_pr_feedback_requests_reviews_and_comments(mock_run):
    mock_run.return_value = _completed("")
    get_pr_feedback(Path("/repo"), "https://x/pr/1")

    cmd = mock_run.call_args.args[0]
    assert "--json" in cmd
    assert cmd[cmd.index("--json") + 1] == "reviews,comments"


@patch("ouroboros.git_ops.subprocess.run")
def test_get_pr_feedback_truncates(mock_run):
    """This text is fed back into a prompt, so it needs a ceiling."""
    mock_run.return_value = _completed("x" * 5000)
    assert len(get_pr_feedback(Path("/repo"), "https://x/pr/1", max_chars=100)) == 100


@patch("ouroboros.git_ops.subprocess.run")
def test_get_pr_feedback_under_the_limit_is_untouched(mock_run):
    mock_run.return_value = _completed("short")
    assert get_pr_feedback(Path("/repo"), "https://x/pr/1", max_chars=100) == "short"


@patch("ouroboros.git_ops.subprocess.run")
def test_get_pr_feedback_empty(mock_run):
    mock_run.return_value = _completed("   \n")
    # "" means the query worked and there was nothing to report.
    assert get_pr_feedback(Path("/repo"), "https://x/pr/1") == ""


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["gh"]),
        FileNotFoundError("gh not installed"),
        subprocess.TimeoutExpired(["gh"], 30),
    ],
)
@patch("ouroboros.git_ops.subprocess.run")
def test_get_pr_feedback_none_on_failure(mock_run, error):
    """None, not "": the caller finalises the record and stops polling it, so
    an unfetchable review would otherwise be lost permanently."""
    mock_run.side_effect = error
    assert get_pr_feedback(Path("/repo"), "https://x/pr/1") is None


def test_every_open_pr_caller_handles_the_unknown_state():
    """The tri-state contract only helps if every call site honours it.

    Two ways to get it wrong: branching directly on the call, or assigning it
    and never distinguishing None from False. The CLI did the second, which an
    `if <call>` check misses.

    Truthiness *after* an explicit comparison is fine -- at that point the
    ambiguous case has already been separated out -- so this requires an
    explicit `is`/`is not` comparison per name rather than forbidding all
    boolean use.
    """
    import ast
    from pathlib import Path as _Path

    CALL = "has_open_improvement_prs"
    src = _Path(__file__).resolve().parent.parent / "src" / "ouroboros"
    offenders = []

    def _is_the_call(node):
        return (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", None)) == CALL
        )

    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if CALL not in text or path.name == "git_ops.py":
            continue
        tree = ast.parse(text)

        for node in ast.walk(tree):
            # Branching straight on the call cannot distinguish None.
            if isinstance(node, (ast.If, ast.IfExp)):
                test = node.test
                if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                    test = test.operand
                if _is_the_call(test):
                    offenders.append(f"{path.name}:{node.lineno} (bare call)")

        # Every name the result is bound to must be compared explicitly.
        bound = {
            target.id: node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and _is_the_call(node.value)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        compared = {
            operand.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
            and any(isinstance(op, (ast.Is, ast.IsNot)) for op in node.ops)
            for operand in [node.left]
            if isinstance(operand, ast.Name)
        }
        for name, lineno in bound.items():
            if name not in compared:
                offenders.append(f"{path.name}:{lineno} ({name} never compared)")

    assert offenders == [], (
        "these read the result as a boolean, so None counts as "
        f"'no open PR': {', '.join(offenders)}"
    )




@patch("ouroboros.git_ops.subprocess.run")
def test_has_open_improvement_prs_saturated_page_is_unknown(mock_run):
    """"None of the first 200" is not "none"."""
    from ouroboros.git_ops import _PR_LIST_LIMIT

    mock_run.return_value = _completed(
        "\n".join(f"feature/pr-{i}" for i in range(_PR_LIST_LIMIT))
    )
    assert has_open_improvement_prs(Path("/repo")) is None


@patch("ouroboros.git_ops.subprocess.run")
def test_has_open_improvement_prs_short_page_is_definitive(mock_run):
    mock_run.return_value = _completed("\n".join(f"feature/pr-{i}" for i in range(5)))
    assert has_open_improvement_prs(Path("/repo")) is False


# -- auto_merge_pr -----------------------------------------------------------

def _gh_error(stderr):
    err = subprocess.CalledProcessError(1, ["gh", "pr", "merge"])
    err.stderr = stderr
    return err


@patch("ouroboros.git_ops.subprocess.run")
def test_auto_merge_pr_uses_auto(mock_run):
    mock_run.return_value = _completed("")
    assert auto_merge_pr(Path("/repo"), "https://gh/pr/1") is True

    cmd = mock_run.call_args.args[0]
    assert cmd[:3] == ["gh", "pr", "merge"]
    assert "--auto" in cmd
    assert "--squash" in cmd


@pytest.mark.parametrize(
    "stderr",
    [
        "X GraphQL: Auto-merge is not allowed for this repository (enablePullRequestAutoMerge)",
        "Pull request Auto-merge is not allowed for this repository",
    ],
)
@patch("ouroboros.git_ops.subprocess.run")
def test_auto_merge_pr_refuses_to_merge_without_checks(mock_run, stderr):
    """No --auto means no CI gate, so the PR must be left open, not merged.

    The old fallback ran a bare `gh pr merge`, which merges immediately and
    waits for nothing -- on a repo with allow_auto_merge=false that was the
    only reachable path, so every autonomous PR landed unchecked.
    """
    mock_run.side_effect = [_gh_error(stderr), _completed("")]

    assert auto_merge_pr(Path("/repo"), "https://gh/pr/1") is False
    # The second side_effect entry must never be consumed: exactly one
    # `gh pr merge` attempt, the one carrying --auto.
    assert mock_run.call_count == 1


@patch("ouroboros.git_ops.subprocess.run")
def test_auto_merge_pr_unrelated_failure_returns_false(mock_run):
    mock_run.side_effect = _gh_error("could not resolve to a PullRequest")
    assert auto_merge_pr(Path("/repo"), "https://gh/pr/1") is False


@patch("ouroboros.git_ops.subprocess.run")
def test_auto_merge_pr_tolerates_missing_stderr(mock_run):
    mock_run.side_effect = _gh_error(None)
    assert auto_merge_pr(Path("/repo"), "https://gh/pr/1") is False


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("gh not installed"),
        subprocess.TimeoutExpired(["gh"], 30),
    ],
)
@patch("ouroboros.git_ops.subprocess.run")
def test_auto_merge_pr_gh_unavailable(mock_run, error):
    mock_run.side_effect = error
    assert auto_merge_pr(Path("/repo"), "https://gh/pr/1") is False


# -- get_pr_checks_status ----------------------------------------------------

@pytest.mark.parametrize("status,returncode", [("pass", 0), ("fail", 1), ("pending", 8)])
@patch("ouroboros.git_ops.subprocess.run")
def test_get_pr_checks_status_returns_the_bucket(mock_run, status, returncode):
    # gh exits non-zero for anything but a green PR (8 while pending) and
    # still prints the jq result.
    mock_run.return_value = _completed(f"{status}\n", returncode=returncode)
    assert get_pr_checks_status(Path("/repo"), "https://gh/pr/1") == status


@patch("ouroboros.git_ops.subprocess.run")
def test_get_pr_checks_status_no_checks_is_pending(mock_run):
    """A PR with no checks has not passed anything.

    `gh pr checks` exits 1 and prints nothing on stdout in this case, so the
    absence of output must not read as "pass" -- nor as "unknown".
    """
    mock_run.return_value = _completed(
        "", returncode=1, stderr="no checks reported on the 'ouroboros/x' branch\n"
    )
    assert get_pr_checks_status(Path("/repo"), "https://gh/pr/1") == "pending"


@patch("ouroboros.git_ops.subprocess.run")
def test_get_pr_checks_status_exit_8_is_pending(mock_run):
    """Exit code 8 is gh's documented "checks pending"."""
    mock_run.return_value = _completed("", returncode=8)
    assert get_pr_checks_status(Path("/repo"), "https://gh/pr/1") == "pending"


@patch("ouroboros.git_ops.subprocess.run")
def test_get_pr_checks_status_unknown_stays_none(mock_run):
    mock_run.return_value = _completed("", returncode=1, stderr="authentication failed\n")
    assert get_pr_checks_status(Path("/repo"), "https://gh/pr/1") is None


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("gh not installed"),
        subprocess.TimeoutExpired(["gh"], 30),
    ],
)
@patch("ouroboros.git_ops.subprocess.run")
def test_get_pr_checks_status_gh_unavailable(mock_run, error):
    mock_run.side_effect = error
    assert get_pr_checks_status(Path("/repo"), "https://gh/pr/1") is None


def _checks_jq_query():
    """The jq expression get_pr_checks_status hands to `gh pr checks -q`."""
    with patch("ouroboros.git_ops.subprocess.run") as mock_run:
        mock_run.return_value = _completed("pass")
        get_pr_checks_status(Path("/repo"), "https://gh/pr/1")
    cmd = mock_run.call_args.args[0]
    return cmd[cmd.index("-q") + 1]


@pytest.mark.parametrize(
    "states,expected",
    [
        ([], "pending"),
        (["SUCCESS"], "pass"),
        (["SUCCESS", "SUCCESS"], "pass"),
        (["SUCCESS", "FAILURE"], "fail"),
        (["FAILURE"], "fail"),
        (["SUCCESS", "IN_PROGRESS"], "pending"),
        (["QUEUED"], "pending"),
    ],
)
def test_checks_jq_query_semantics(states, expected):
    """Run the real jq expression: an empty check list must not read "pass".

    jq's all() is true on an empty array, so `all(. == "SUCCESS")` on its own
    calls a PR whose checks have not started green.
    """
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq not installed")

    out = subprocess.run(
        [jq, "-r", _checks_jq_query()],
        input=json.dumps([{"state": s} for s in states]),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert out.stdout.strip() == expected
