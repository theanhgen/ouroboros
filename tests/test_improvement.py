"""Tests for improvement engine."""

import json

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from ouroboros.config import SafetyConfig
from ouroboros.improvement import (
    ImprovementTask,
    CodeChange,
    ImprovementResult,
    _build_failed_attempts_context,
    _build_success_rate_context,
    _is_path_allowed,
    _validate_changes,
    _count_changed_lines,
    apply_changes,
    revert_changes,
    run_improvement_cycle,
    validate_improvement,
)
from ouroboros.evaluation import EvaluationRecord
from ouroboros.model_defaults import DEFAULT_OPENAI_MODEL
from ouroboros.test_runner import RunnerOutcome


def test_immutable_files():
    """The shipped config is the immutable-file list; the enforcement gate
    reads it rather than a second hardcoded copy (#112)."""
    config = SafetyConfig()
    for name in ("config.py", "improvement.py", "git_ops.py", "evaluation.py",
                 "policies.py"):
        assert name in config.forbidden_modification_paths
        assert _is_path_allowed(f"src/ouroboros/{name}", config) is False


def test_is_path_allowed():
    config = SafetyConfig()
    assert _is_path_allowed("src/ouroboros/llm.py", config) is True
    assert _is_path_allowed("tests/test_foo.py", config) is True
    assert _is_path_allowed("src/ouroboros/config.py", config) is False
    assert _is_path_allowed("src/ouroboros/improvement.py", config) is False
    assert _is_path_allowed("README.md", config) is False
    assert _is_path_allowed("setup.py", config) is False


def test_prompts_module_stays_agent_editable():
    """Prompt text must stay something the reviewed improvement cycle can edit.

    self_improve.py carried a second prompt writer (_write_prompt) that ran no
    tests, no peer review and no daily cap. It was deleted for #109 on the
    grounds that every prompt already lives in src/ouroboros/prompts.py, which
    the normal cycle may rewrite under all of those gates. Forbidding that file
    would take prompt self-editing away again, silently.
    """
    config = SafetyConfig()
    assert _is_path_allowed("src/ouroboros/prompts.py", config) is True


def test_is_path_allowed_forbidden_path_and_prefix():
    config = SafetyConfig(
        forbidden_modification_paths=(
            "src/ouroboros/secret.py",
            "src/ouroboros/forbidden_dir/",
        )
    )

    assert _is_path_allowed("src/ouroboros/llm.py", config) is True
    assert _is_path_allowed("src/ouroboros/secret.py", config) is False
    assert _is_path_allowed("src/ouroboros/forbidden_dir/nested.py", config) is False
    assert _is_path_allowed("src/ouroboros/forbidden_dir_not/nested.py", config) is True


def test_validate_changes_ok():
    config = SafetyConfig()
    changes = [
        CodeChange("src/ouroboros/llm.py", "old", "new", "fix bug"),
    ]
    violations = _validate_changes(changes, config)
    assert violations == []


def test_validate_changes_forbidden_file():
    config = SafetyConfig()
    changes = [
        CodeChange("src/ouroboros/config.py", "old", "new", "modify config"),
    ]
    violations = _validate_changes(changes, config)
    assert len(violations) == 1
    assert "Forbidden" in violations[0]


def test_validate_changes_too_many_files():
    config = SafetyConfig(max_changed_files_per_pr=2)
    changes = [
        CodeChange("src/ouroboros/a.py", "a", "b", "d"),
        CodeChange("src/ouroboros/b.py", "a", "b", "d"),
        CodeChange("src/ouroboros/c.py", "a", "b", "d"),
    ]
    violations = _validate_changes(changes, config)
    assert any("Too many files" in v for v in violations)


def test_validate_changes_too_many_lines():
    config = SafetyConfig(max_lines_changed_per_pr=5)
    original = "line1\nline2\nline3\n"
    new_content = "changed1\nchanged2\nchanged3\nnew4\nnew5\nnew6\nnew7\nnew8\n"
    changes = [
        CodeChange("src/ouroboros/foo.py", original, new_content, "big change"),
    ]
    violations = _validate_changes(changes, config)
    assert any("Too many lines" in v for v in violations)


def test_validate_refuses_when_baseline_tests_did_not_run(tmp_path):
    # Regression: a misconfigured runner (e.g. missing pytest-cov -> rc=4, 0
    # collected) must NOT be treated as "no regression" and merged. The loop
    # must refuse to proceed when it cannot actually validate.
    task = ImprovementTask("t", "fix_bug", "x", ["src/ouroboros/x.py"], "")
    changes = [CodeChange("src/ouroboros/x.py", "a\n", "b\n", "d")]
    broken = RunnerOutcome(passed=0, failed=0, errors=0, returncode=4)
    with patch("ouroboros.improvement.run_tests", return_value=broken) as rt:
        result = validate_improvement(task, changes, tmp_path, config=SafetyConfig())
    assert result.status == "failed"
    assert "Cannot validate" in result.details
    # It must bail before ever applying changes (only the baseline run happened).
    assert rt.call_count == 1


def test_count_changed_lines():
    assert _count_changed_lines("a\nb\nc\n", "a\nb\nc\n") == 0
    assert _count_changed_lines("a\nb\n", "a\nX\n") == 1
    assert _count_changed_lines("a\n", "a\nb\n") == 1


def test_count_changed_lines_early_insertion_not_overcounted():
    # Regression: inserting a few lines near the top must not mis-align and
    # report the whole file as changed (the old index-by-index bug counted 572
    # for an 11-line agent refactor, tripping the line cap).
    body = "".join(f"line{i}\n" for i in range(200))
    original = "import os\n" + body
    new_content = "import os\nimport sys\n\ndef helper():\n    return 1\n" + body
    assert _count_changed_lines(original, new_content) <= 5


def test_improvement_task_from_llm_response():
    data = {
        "task_type": "fix_test",
        "description": "Fix test_foo",
        "target_files": ["tests/test_foo.py"],
        "evidence": "test_foo fails with AssertionError",
    }
    task = ImprovementTask.from_llm_response(data)
    assert task.task_type == "fix_test"
    assert task.description == "Fix test_foo"
    assert task.target_files == ["tests/test_foo.py"]
    assert len(task.task_id) == 8


def test_apply_changes(tmp_path):
    # Create allowed directory structure
    src_dir = tmp_path / "src" / "ouroboros"
    src_dir.mkdir(parents=True)
    target = src_dir / "foo.py"
    target.write_text("original")

    changes = [
        CodeChange("src/ouroboros/foo.py", "original", "modified", "test"),
    ]

    with patch("ouroboros.improvement.SafetyConfig") as mock_config:
        mock_config.return_value = SafetyConfig()
        apply_changes(changes, tmp_path)

    assert target.read_text() == "modified"


def test_apply_changes_forbidden(tmp_path):
    changes = [
        CodeChange("src/ouroboros/config.py", "old", "new", "hack"),
    ]
    try:
        apply_changes(changes, tmp_path)
        assert False, "Should have raised PermissionError"
    except PermissionError:
        pass


def test_revert_changes(tmp_path):
    src_dir = tmp_path / "src" / "ouroboros"
    src_dir.mkdir(parents=True)
    target = src_dir / "foo.py"
    target.write_text("modified")

    changes = [
        CodeChange(
            "src/ouroboros/foo.py", "original", "modified", "test",
            existed_before=True,
        ),
    ]
    revert_changes(changes, tmp_path)

    assert target.read_text() == "original"


def test_revert_new_file(tmp_path):
    src_dir = tmp_path / "src" / "ouroboros"
    src_dir.mkdir(parents=True)
    target = src_dir / "new_file.py"
    target.write_text("new content")

    changes = [
        CodeChange(
            "src/ouroboros/new_file.py", "", "new content", "new file",
            existed_before=False,
        ),
    ]
    revert_changes(changes, tmp_path)
    assert not target.exists()  # was not on disk before, so remove it


def test_revert_keeps_an_existing_empty_file(tmp_path):
    """A tracked-but-empty file carries the same original_content ("") as a
    brand new one, so revert must not infer "newly created" from emptiness and
    unlink it (#91)."""
    src_dir = tmp_path / "src" / "ouroboros"
    src_dir.mkdir(parents=True)
    target = src_dir / "empty.py"
    target.write_text("")

    changes = [
        CodeChange("src/ouroboros/empty.py", "", "VALUE = 1\n", "fill it in"),
    ]
    apply_changes(changes, tmp_path)
    assert target.read_text() == "VALUE = 1\n"

    revert_changes(changes, tmp_path)

    assert target.exists(), "revert deleted a file that existed before the change"
    assert target.read_text() == ""


def test_revert_removes_a_new_file_touched_by_two_changes(tmp_path):
    """Two entries can name the same new file (create, then edit it). The
    second one is applied to a file the first just created, so it records
    existed_before=True. Reverting forwards would unlink it for the first
    entry and then write it back for the second, leaving a stray file and a
    dirty worktree that blocks every later cycle."""
    src_dir = tmp_path / "src" / "ouroboros"
    src_dir.mkdir(parents=True)
    target = src_dir / "brand_new.py"

    changes = [
        CodeChange("src/ouroboros/brand_new.py", "", "A = 1\n", "create"),
        CodeChange("src/ouroboros/brand_new.py", "", "A = 2\n", "edit it"),
    ]
    apply_changes(changes, tmp_path)
    assert [c.existed_before for c in changes] == [False, True]

    revert_changes(changes, tmp_path)

    assert not target.exists(), "revert left behind a file that did not exist before"


def test_revert_skips_changes_that_were_never_applied(tmp_path):
    """apply_changes stamps existed_before; a change that never reached it
    was never written, so revert must not touch that file. Guessing from an
    empty original_content would unlink a file already on disk."""
    src_dir = tmp_path / "src" / "ouroboros"
    src_dir.mkdir(parents=True)
    untouched = src_dir / "untouched.py"
    untouched.write_text("KEEP = 1\n")

    revert_changes(
        [CodeChange("src/ouroboros/untouched.py", "", "NEW = 1\n", "never applied")],
        tmp_path,
    )

    assert untouched.read_text() == "KEEP = 1\n"


def test_revert_does_not_write_through_a_symlink_swapped_in_after_apply(tmp_path):
    """Tests run between apply and revert. If the applied file is replaced by
    a symlink pointing outside the repository, rebuilding repo_root /
    file_path at rollback writes original_content through it (#142)."""
    import os

    repo = tmp_path / "repo"
    pkg = repo / "src" / "ouroboros"
    pkg.mkdir(parents=True)
    target = pkg / "foo.py"
    target.write_text("original\n")
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 1\n")

    changes = [CodeChange("src/ouroboros/foo.py", "original\n", "modified\n", "d")]
    apply_changes(changes, repo)

    target.unlink()
    os.symlink(outside, target)

    revert_changes(changes, repo)

    assert outside.read_text() == "SECRET = 1\n"


def test_revert_does_not_write_into_an_immutable_file_via_a_swapped_symlink(tmp_path):
    """Landing inside the repository is not enough at rollback either: a
    symlink swapped in for the applied file can name config.py (#142)."""
    import os

    repo = tmp_path / "repo"
    pkg = repo / "src" / "ouroboros"
    pkg.mkdir(parents=True)
    (pkg / "config.py").write_text("ALLOW_SELF_MODIFICATION = False\n")
    target = pkg / "foo.py"
    target.write_text("ALLOW_SELF_MODIFICATION = True\n")

    changes = [CodeChange(
        "src/ouroboros/foo.py", "ALLOW_SELF_MODIFICATION = True\n", "X = 1\n", "d",
    )]
    apply_changes(changes, repo)

    target.unlink()
    os.symlink(pkg / "config.py", target)

    revert_changes(changes, repo)

    assert (pkg / "config.py").read_text() == "ALLOW_SELF_MODIFICATION = False\n"


def test_revert_does_not_unlink_through_a_parent_directory_swapped_for_a_symlink(tmp_path):
    """A new file is undone by unlinking it. If its parent directory was
    replaced by a symlink after the apply, the unlink lands outside (#142)."""
    import os

    repo = tmp_path / "repo"
    pkg = repo / "src" / "ouroboros" / "sub"
    pkg.mkdir(parents=True)
    changes = [CodeChange("src/ouroboros/sub/new.py", "", "A = 1\n", "create")]
    apply_changes(changes, repo)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "new.py").write_text("KEEP = 1\n")
    (pkg / "new.py").unlink()
    pkg.rmdir()
    os.symlink(outside, pkg)

    revert_changes(changes, repo)

    assert (outside / "new.py").read_text() == "KEEP = 1\n"


def test_revert_without_applied_path_does_not_follow_a_symlink(tmp_path):
    """A change with no applied_path falls back to repo_root / file_path.
    Resolving that before looking for symlinks would follow one to
    .git/config and overwrite it (#142)."""
    import os

    repo = tmp_path / "repo"
    pkg = repo / "src" / "ouroboros"
    pkg.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("GIT\n")
    os.symlink(repo / ".git" / "config", pkg / "foo.py")

    revert_changes(
        [CodeChange("src/ouroboros/foo.py", "orig\n", "mod\n", "d",
                    existed_before=True)],
        repo,
    )

    assert (repo / ".git" / "config").read_text() == "GIT\n"


def test_revert_still_restores_when_the_repo_root_is_a_symlink(tmp_path, monkeypatch):
    """The symlink guard must not refuse a legitimate rollback when the
    repository itself is reached through a symlink (macOS /var ->
    /private/var) or given as a relative path."""
    import os

    real = tmp_path / "real"
    pkg = real / "src" / "ouroboros"
    pkg.mkdir(parents=True)
    (pkg / "foo.py").write_text("original\n")
    os.symlink(real, tmp_path / "link")

    via_link = [CodeChange("src/ouroboros/foo.py", "original\n", "modified\n", "d")]
    apply_changes(via_link, tmp_path / "link")
    assert (pkg / "foo.py").read_text() == "modified\n"
    revert_changes(via_link, tmp_path / "link")
    assert (pkg / "foo.py").read_text() == "original\n"

    monkeypatch.chdir(tmp_path)
    relative = [CodeChange("src/ouroboros/foo.py", "original\n", "modified\n", "d")]
    apply_changes(relative, Path("link"))
    assert (pkg / "foo.py").read_text() == "modified\n"
    revert_changes(relative, Path("link"))
    assert (pkg / "foo.py").read_text() == "original\n"


def test_revert_does_not_write_through_a_hard_link_swapped_in_after_apply(tmp_path):
    """A hard link is not a symlink, so the lstat walk cannot see it: writing
    through a name hard-linked to config.py rewrites config.py (#142)."""
    import os

    repo = tmp_path / "repo"
    pkg = repo / "src" / "ouroboros"
    pkg.mkdir(parents=True)
    (pkg / "config.py").write_text("ALLOW_SELF_MODIFICATION = False\n")
    target = pkg / "foo.py"
    target.write_text("original\n")

    changes = [CodeChange("src/ouroboros/foo.py", "original\n", "modified\n", "d")]
    apply_changes(changes, repo)

    target.unlink()
    os.link(pkg / "config.py", target)

    assert revert_changes(changes, repo) == []

    assert (pkg / "config.py").read_text() == "ALLOW_SELF_MODIFICATION = False\n"
    assert target.read_text() == "original\n"


def test_revert_replaces_a_fifo_instead_of_blocking_on_it(tmp_path):
    """Opening a FIFO for writing blocks until a reader appears, which would
    hang the rollback forever (#142)."""
    import os

    repo = tmp_path / "repo"
    pkg = repo / "src" / "ouroboros"
    pkg.mkdir(parents=True)
    target = pkg / "foo.py"
    target.write_text("original\n")

    changes = [CodeChange("src/ouroboros/foo.py", "original\n", "modified\n", "d")]
    apply_changes(changes, repo)

    target.unlink()
    os.mkfifo(target)

    assert revert_changes(changes, repo) == []
    assert target.read_text() == "original\n"


def test_revert_refuses_and_reports_a_directory_swapped_in_after_apply(tmp_path):
    """A directory where the file was cannot be written or unlinked; the
    revert reports it instead of crashing mid-rollback (#142)."""
    repo = tmp_path / "repo"
    pkg = repo / "src" / "ouroboros"
    pkg.mkdir(parents=True)
    changes = [
        CodeChange("src/ouroboros/other.py", "", "B = 1\n", "create"),
        CodeChange("src/ouroboros/new.py", "", "A = 1\n", "create"),
    ]
    apply_changes(changes, repo)

    (pkg / "new.py").unlink()
    (pkg / "new.py").mkdir()

    assert revert_changes(changes, repo) == ["src/ouroboros/new.py"]
    assert (pkg / "new.py").is_dir()
    assert not (pkg / "other.py").exists()


def test_revert_without_applied_path_honours_forbidden_paths(tmp_path):
    """The no-applied_path fallback must refuse what apply_changes would:
    config.py is immutable (#142)."""
    repo = tmp_path / "repo"
    pkg = repo / "src" / "ouroboros"
    pkg.mkdir(parents=True)
    (pkg / "config.py").write_text("ALLOW_SELF_MODIFICATION = False\n")

    refused = revert_changes(
        [CodeChange("src/ouroboros/config.py", "X = 1\n", "Y = 1\n", "d",
                    existed_before=True)],
        repo,
    )

    assert refused == ["src/ouroboros/config.py"]
    assert (pkg / "config.py").read_text() == "ALLOW_SELF_MODIFICATION = False\n"


@patch("ouroboros.improvement.run_tests")
@patch("ouroboros.improvement.revert_changes", return_value=["src/ouroboros/x.py"])
def test_validate_improvement_does_not_report_a_refused_revert_as_reverted(
        mock_revert, mock_run_tests):
    mock_run_tests.side_effect = [
        RunnerOutcome(passed=5, failed=0, errors=0, returncode=0),
        RunnerOutcome(passed=3, failed=2, errors=0, returncode=1),
    ]
    task = ImprovementTask("abc", "fix_bug", "fix it", ["src/ouroboros/x.py"], "broken")
    changes = [CodeChange("src/ouroboros/x.py", "old", "new", "fix")]

    with patch("ouroboros.improvement.apply_changes"):
        result = validate_improvement(task, changes, Path("/tmp/repo"))

    assert result.status == "failed"
    assert "src/ouroboros/x.py" in result.details


def test_build_failed_attempts_context_uses_outcome_only():
    history = [
        EvaluationRecord(
            task_id="a",
            task_type="fix_bug",
            description="Do not repeat this fix",
            outcome="failed",
            feedback="Broke the CLI",
        ),
        EvaluationRecord(
            task_id="b",
            task_type="add_test",
            description="Successful test addition",
            outcome="success",
        ),
    ]

    context = _build_failed_attempts_context(history)

    assert "Do not repeat this fix" in context
    assert "Broke the CLI" in context
    assert "Successful test addition" not in context


def test_build_success_rate_context_counts_success_outcomes():
    history = [
        EvaluationRecord(task_id="a", task_type="fix_bug", description="one", outcome="success"),
        EvaluationRecord(task_id="b", task_type="fix_bug", description="two", outcome="merged"),
        EvaluationRecord(task_id="c", task_type="fix_bug", description="three", outcome="failed"),
        EvaluationRecord(task_id="d", task_type="add_test", description="four", outcome="closed"),
    ]

    context = _build_success_rate_context(history)

    assert "fix_bug: 2/3 (66%)" in context
    assert "add_test: 0/1 (0%)" in context


@patch("ouroboros.improvement.record_improvement")
@patch("ouroboros.improvement.plan_improvement", return_value=(None, None))
@patch("ouroboros.improvement.identify_improvements")
@patch("ouroboros.improvement.run_tests")
@patch("ouroboros.improvement.get_codebase_summary", return_value="summary")
@patch("ouroboros.improvement.load_history", return_value=[])
@patch("ouroboros.improvement.git_ops.has_open_improvement_prs", return_value=False)
@patch("ouroboros.improvement.improvements_today", return_value=0)
@patch("ouroboros.improvement.get_repo_root")
def test_run_improvement_cycle_returns_failure_when_plan_generation_fails(
    mock_repo_root,
    _mock_today,
    _mock_has_open_prs,
    _mock_load_history,
    _mock_summary,
    mock_run_tests,
    mock_identify,
    _mock_plan,
    mock_record,
    tmp_path,
):
    mock_repo_root.return_value = tmp_path
    mock_run_tests.return_value = RunnerOutcome(passed=5, failed=0, errors=0, returncode=0)
    mock_identify.return_value = ImprovementTask(
        "abc12345",
        "fix_bug",
        "Repair the CLI status output",
        ["src/ouroboros/cli.py"],
        "The CLI status output omits scheduler state.",
    )
    
    mock_client = MagicMock()
    # Mock the tool calling response
    mock_msg = MagicMock()
    mock_msg.tool_calls = None
    mock_msg.content = json.dumps({
        "task_type": "fix_bug",
        "description": "desc",
        "target_files": [],
        "evidence": "ev"
    })
    mock_client.chat.completions.create.return_value.choices = [MagicMock(message=mock_msg)]
    mock_client.chat.completions.create.return_value.usage = None

    result = run_improvement_cycle(client=mock_client, state={}, config=SafetyConfig(), model=DEFAULT_OPENAI_MODEL)

    assert result is not None
    assert result.status == "failed"
    assert "implementation plan" in result.details
    mock_record.assert_called_once()


@patch("ouroboros.improvement.run_tests")
def test_validate_improvement_success(mock_run_tests):
    mock_run_tests.side_effect = [
        RunnerOutcome(passed=5, failed=0, errors=0, returncode=0),  # before
        RunnerOutcome(passed=6, failed=0, errors=0, returncode=0),  # after
    ]

    task = ImprovementTask("abc", "add_test", "add test", ["tests/test_x.py"], "needs test")
    changes = [
        CodeChange("tests/test_x.py", "", "def test_new(): pass", "add test"),
    ]

    with patch("ouroboros.improvement.apply_changes"):
        result = validate_improvement(task, changes, Path("/tmp/repo"))

    assert result.status == "success"
    assert result.test_before.passed == 5
    assert result.test_after.passed == 6


@patch("ouroboros.improvement.run_tests")
@patch("ouroboros.improvement.revert_changes", return_value=[])
def test_validate_improvement_regression(mock_revert, mock_run_tests):
    mock_run_tests.side_effect = [
        RunnerOutcome(passed=5, failed=0, errors=0, returncode=0),  # before
        RunnerOutcome(passed=3, failed=2, errors=0, returncode=1),  # after - regression!
    ]

    task = ImprovementTask("abc", "fix_bug", "fix it", ["src/ouroboros/x.py"], "broken")
    changes = [
        CodeChange("src/ouroboros/x.py", "old", "new", "fix"),
    ]

    with patch("ouroboros.improvement.apply_changes"):
        result = validate_improvement(task, changes, Path("/tmp/repo"))

    assert result.status == "reverted"
    mock_revert.assert_called_once()


# -- generated code is gated on imports before it is applied (#52) -----------

def _change(path="src/ouroboros/thing.py", new="", old=""):
    from ouroboros.improvement import CodeChange

    return CodeChange(
        file_path=path, original_content=old, new_content=new, description="d"
    )


def test_a_change_importing_a_blocked_module_is_refused():
    """forbidden_import_modules had no call site, so the setting did nothing:
    a CodeChange containing `import pickle` went straight to apply and test."""
    from ouroboros.config import SafetyConfig
    from ouroboros.improvement import _validate_changes

    violations = _validate_changes(
        [_change(new="import pickle\n\nDATA = 1\n")], SafetyConfig()
    )
    assert any("pickle" in v for v in violations), violations


def test_a_clean_change_still_passes():
    from ouroboros.config import SafetyConfig
    from ouroboros.improvement import _validate_changes

    assert _validate_changes(
        [_change(new="import json\n\nDATA = 1\n")], SafetyConfig()
    ) == []


def test_a_dynamic_import_is_reported():
    from ouroboros.config import SafetyConfig
    from ouroboros.improvement import _validate_changes

    violations = _validate_changes(
        [_change(new="m = __import__('pickle')\n")], SafetyConfig()
    )
    assert any("Dynamic import" in v for v in violations), violations


def test_non_python_changes_are_not_reported_as_unparseable():
    """A JSON or Markdown change is not source; reporting it would be a false
    positive that blocks legitimate work."""
    from ouroboros.config import SafetyConfig
    from ouroboros.improvement import _validate_changes

    for path, body in (
        ("docs/wiki/notes.md", "# not python at all: import pickle"),
        ("config/sample.json", '{"a": 1}'),
    ):
        violations = _validate_changes([_change(path=path, new=body)], SafetyConfig())
        # Scope is a separate gate; the claim here is only that nothing was
        # handed to the parser.
        assert not [
            v for v in violations
            if "Unparseable" in v or "import" in v.lower()
        ], violations


def test_blocked_code_never_reaches_apply(monkeypatch, tmp_path):
    """The gate has to sit before anything is written, not after.

    A baseline test run does happen first, but on the unmodified tree -- the
    generated source has not been written at that point.
    """
    from ouroboros import improvement
    from ouroboros.improvement import ImprovementTask, validate_improvement
    from ouroboros.test_runner import RunnerOutcome

    applied = []
    runs = []
    monkeypatch.setattr(
        improvement, "apply_changes", lambda *a, **kw: applied.append(a) or True
    )
    monkeypatch.setattr(
        improvement, "run_tests",
        lambda *a, **kw: runs.append(bool(applied))
        or RunnerOutcome(passed=1, failed=0, errors=0, returncode=0),
    )

    result = validate_improvement(
        ImprovementTask("t1", "fix_bug", "d", [], "e"),
        [_change(new="import ctypes\n")],
        tmp_path,
    )

    assert applied == [], "the change was applied despite a policy violation"
    assert runs == [False], "tests ran against the generated code"
    assert result.status == "failed"
    assert "ctypes" in result.details


@pytest.mark.parametrize("path", [
    "src/ouroboros/x.pyi",
    "src/ouroboros/x.pyw",
    "src/ouroboros/x.PY",
])
def test_python_by_any_of_its_suffixes_is_gated(path):
    """A case-insensitive filesystem makes x.PY the same file as x.py, and a
    stub is still source."""
    from ouroboros.config import SafetyConfig
    from ouroboros.improvement import _validate_changes

    violations = _validate_changes(
        [_change(path=path, new="import pickle\n")], SafetyConfig()
    )
    assert any("pickle" in v for v in violations), violations


def test_the_retry_flow_refuses_a_blocked_change(monkeypatch, tmp_path):
    """Behavioural, not a source grep: a retry must not launder a change the
    first attempt would have been refused."""
    from ouroboros import improvement
    from ouroboros.improvement import ImprovementTask
    from ouroboros.test_runner import RunnerOutcome

    applied = []
    monkeypatch.setattr(
        improvement, "apply_changes", lambda *a, **kw: applied.append(a) or True
    )
    monkeypatch.setattr(
        improvement.llm, "generate_code",
        lambda *a, **kw: (
            [{"file_path": "src/ouroboros/x.py",
              "new_content": "import pickle\n",
              "description": "d"}],
            None,
        ),
    )

    result = improvement._retry_with_root_cause(
        client=MagicMock(),
        task=ImprovementTask("t1", "fix_bug", "d", [], "e"),
        original_changes=[_change(new="VALUE = 1\n")],
        test_before=RunnerOutcome(passed=1, failed=0, errors=0, returncode=0),
        test_after=RunnerOutcome(passed=0, failed=1, errors=0, returncode=1),
        config=SafetyConfig(),
        repo_root=tmp_path,
    )

    assert applied == [], "the retry applied a change the gate should refuse"
    assert result is None


@pytest.mark.parametrize("retry_test", [
    # hollow: nothing ran, so 0 failed / 0 errors looks "no worse"
    RunnerOutcome(passed=0, failed=0, errors=0, returncode=4),
    # coverage dropped past the gate validate_improvement enforces
    RunnerOutcome(passed=10, failed=0, errors=0, returncode=0, coverage_percent=80.0),
], ids=["hollow-run", "coverage-drop"])
def test_the_retry_flow_does_not_accept_what_the_first_attempt_would_revert(
    monkeypatch, tmp_path, retry_test,
):
    """#107: the retry used to check only failure/error counts, so a hollow
    run or a coverage regression came back as success and went on to a PR."""
    from ouroboros import improvement

    reverted = []
    monkeypatch.setattr(improvement, "apply_changes", lambda *a, **kw: None)
    monkeypatch.setattr(
        improvement, "revert_changes", lambda changes, root: reverted.append(changes)
    )
    monkeypatch.setattr(improvement, "run_tests", lambda root: retry_test)
    monkeypatch.setattr(
        improvement.llm, "generate_code",
        lambda *a, **kw: (
            [{"file_path": "src/ouroboros/thing.py",
              "new_content": "VALUE = 2\n",
              "description": "d"}],
            None,
        ),
    )

    result = improvement._retry_with_root_cause(
        client=MagicMock(),
        task=ImprovementTask("t1", "fix_bug", "d", [], "e"),
        original_changes=[_change(new="VALUE = 1\n")],
        test_before=RunnerOutcome(passed=10, failed=0, errors=0, returncode=0,
                                  coverage_percent=90.0),
        test_after=RunnerOutcome(passed=9, failed=1, errors=0, returncode=1),
        config=SafetyConfig(),
        repo_root=tmp_path,
    )

    assert result is None, "the retry accepted a run the first attempt would revert"
    assert len(reverted) == 1, "the corrected code was left applied"


def test_an_extensionless_script_with_a_python_shebang_is_gated():
    """Suffix alone misses a script named like a command."""
    from ouroboros.config import SafetyConfig
    from ouroboros.improvement import _validate_changes

    violations = _validate_changes(
        [_change(
            path="src/ouroboros/runner",
            new="#!/usr/bin/env python3\nimport pickle\n",
        )],
        SafetyConfig(),
    )
    assert any("pickle" in v for v in violations), violations


def test_an_extensionless_non_python_file_is_still_not_parsed():
    from ouroboros.config import SafetyConfig
    from ouroboros.improvement import _validate_changes

    violations = _validate_changes(
        [_change(path="src/ouroboros/LICENSE", new="not code: import pickle")],
        SafetyConfig(),
    )
    assert not [v for v in violations if "Unparseable" in v or "import" in v.lower()]


def test_a_trailing_space_does_not_hide_a_python_file():
    """Some filesystems strip it on write, so "foo.py " lands as "foo.py"."""
    from ouroboros.config import SafetyConfig
    from ouroboros.improvement import _validate_changes

    violations = _validate_changes(
        [_change(path="src/ouroboros/foo.py ", new="import pickle\n")],
        SafetyConfig(),
    )
    assert any("pickle" in v for v in violations), violations


def _fail(test, tb, file="tests/test_x.py", message=""):
    from ouroboros.test_runner import FailureDetail

    return FailureDetail(
        test_name=test, file=file, line=None, message=message, traceback=tb
    )


def _helper_tb(caller_line, got):
    """--tb=short output for a test that dies inside the same broken helper."""
    return (
        f"tests/test_x.py:{caller_line}: in test\n    helper()\n"
        "src/ouroboros/thing.py:42: in helper\n    assert value == 2\n"
        f"E   AssertionError: assert {got} == 2"
    )


def _retry_prompt_for(monkeypatch, tmp_path, failure_details):
    """The prompt _retry_with_root_cause sends to the LLM for these failures."""
    from ouroboros import improvement

    prompts = []
    monkeypatch.setattr(
        improvement.llm, "generate_code",
        lambda client, prompt, *a, **kw: prompts.append(prompt) or ([], None),
    )
    improvement._retry_with_root_cause(
        client=MagicMock(),
        task=ImprovementTask("t1", "fix_bug", "d", [], "e"),
        original_changes=[_change(new="VALUE = 1\n")],
        test_before=RunnerOutcome(passed=3, failed=0, errors=0, returncode=0),
        test_after=RunnerOutcome(
            passed=0, failed=len(failure_details), errors=0, returncode=1,
            failure_details=failure_details,
        ),
        config=SafetyConfig(),
        repo_root=tmp_path,
    )
    assert len(prompts) == 1
    return prompts[0]


def test_retry_prompt_clusters_duplicate_failures_into_one_root_cause(monkeypatch, tmp_path):
    """Three tests failing in the same helper are one cause, not three (#134):
    the retry prompt must say so and point the next attempt at it."""
    prompt = _retry_prompt_for(monkeypatch, tmp_path, [
        _fail("test_a", _helper_tb(10, 3)),
        _fail("test_b", _helper_tb(20, 4)),
        _fail("test_c", _helper_tb(30, 5)),
    ])

    assert "## Failure triage" in prompt
    assert "3 failure(s) in 1 distinct root cause cluster(s)" in prompt
    assert "[3x] AssertionError: assert N == N at src/ouroboros/thing.py::helper" in prompt
    assert "Suggested next experiment: 3 of 3 failures share" in prompt


def test_retry_prompt_ranks_distinct_root_causes_largest_first(monkeypatch, tmp_path):
    lone = (
        "tests/test_y.py:5: in test_lone\n    parse('x')\n"
        "src/ouroboros/parse.py:9: in parse\n    raise ValueError(f'bad {s!r}')\n"
        "E   ValueError: bad 'x'"
    )
    prompt = _retry_prompt_for(monkeypatch, tmp_path, [
        _fail("test_lone", lone, file="tests/test_y.py"),
        _fail("test_a", _helper_tb(10, 3)),
        _fail("test_b", _helper_tb(20, 4)),
    ])

    assert "3 failure(s) in 2 distinct root cause cluster(s)" in prompt
    helper = prompt.index("[2x] AssertionError")
    parse = prompt.index("[1x] ValueError: bad '?' at src/ouroboros/parse.py::parse")
    assert helper < parse, "the larger cluster must be listed first"
    assert "2 of 3 failures share" in prompt


def test_retry_prompt_without_failure_details_keeps_the_summary_fallback(monkeypatch, tmp_path):
    prompt = _retry_prompt_for(monkeypatch, tmp_path, [])

    assert "## Failure triage" not in prompt
    assert "Tests: 0 passed, 0 failed" in prompt
    assert "## What was attempted" in prompt


def test_failure_triage_is_empty_without_details():
    from ouroboros.improvement import _format_failure_triage

    assert _format_failure_triage(RunnerOutcome(failed=2, returncode=1)) == ""


def test_failure_triage_uses_file_and_message_when_there_is_no_traceback():
    from ouroboros.improvement import _format_failure_triage

    out = _format_failure_triage(RunnerOutcome(failed=2, returncode=1, failure_details=[
        _fail("", "", file="tests/test_a.py", message="ImportError: no module 'x'"),
        _fail("", "", file="tests/test_b.py", message="ImportError: no module 'y'"),
    ]))

    assert "2 distinct root cause cluster(s)" in out
    assert "ImportError: no module '?' at tests/test_a.py" in out
    assert "no failure repeats" in out


def _tool_runner_repo(tmp_path):
    """A repo_root whose src/ holds one grep-able module."""
    src_dir = tmp_path / "src" / "ouroboros"
    src_dir.mkdir(parents=True)
    (src_dir / "sample.py").write_text("def findable_symbol():\n    return 1\n")
    return tmp_path


def test_grep_codebase_reports_no_matches_instead_of_an_error(tmp_path):
    """grep exits 1 when it found nothing -- that is an answer, not breakage."""
    from ouroboros.improvement import ToolRunner

    out = ToolRunner(_tool_runner_repo(tmp_path)).execute(
        "grep_codebase", {"pattern": "ZZZ_NO_MATCH_ZZZ"}
    )
    assert out == "No matches found."


def test_grep_codebase_returns_the_matching_lines(tmp_path):
    from ouroboros.improvement import ToolRunner

    out = ToolRunner(_tool_runner_repo(tmp_path)).execute(
        "grep_codebase", {"pattern": "findable_symbol"}
    )
    assert "sample.py" in out and "findable_symbol" in out


def test_grep_codebase_still_reports_a_real_failure(tmp_path):
    """Exit >=2 -- here an unbalanced bracket -- stays an error, with grep's own text."""
    from ouroboros.improvement import ToolRunner

    out = ToolRunner(_tool_runner_repo(tmp_path)).execute(
        "grep_codebase", {"pattern": "["}
    )
    assert out.startswith("Error running grep:")
    assert out != "Error running grep:"
    assert "No matches found." not in out


def test_stale_task_type_names_the_stuck_type_instead_of_blocking():
    from types import SimpleNamespace
    from ouroboros.improvement import _stale_task_type

    now = 1_800_000_000
    rec = lambda t, before, after, age=3600: SimpleNamespace(
        timestamp=now - age, task_type=t, test_delta={"before": before, "after": after}
    )
    assert _stale_task_type([], now) is None
    assert _stale_task_type([rec("fix_bug", 1, 1), rec("fix_bug", 1, 1)], now) == "fix_bug"
    # One of the two made progress: not stale.
    assert _stale_task_type([rec("fix_bug", 1, 2), rec("fix_bug", 1, 1)], now) is None
    # Older than the 7-day window: ignored.
    old = 8 * 86400
    assert _stale_task_type([rec("fix_bug", 1, 1, old), rec("fix_bug", 1, 1, old)], now) is None


def test_stale_task_type_ignores_attempts_that_never_ran_tests():
    """Failures before the test run (quota, token cap) are not 'no progress'."""
    from types import SimpleNamespace
    from ouroboros.improvement import _stale_task_type

    now = 1_800_000_000
    zero = {"passed": 0, "failed": 0, "errors": 0}
    untested = SimpleNamespace(
        timestamp=now - 60, task_type="fix_bug", test_delta={"before": zero, "after": zero}
    )
    assert _stale_task_type([untested, untested], now) is None
    empty = SimpleNamespace(timestamp=now - 60, task_type="fix_bug", test_delta={})
    assert _stale_task_type([empty, empty], now) is None
    ran = {"passed": 10, "failed": 0, "errors": 0}
    tested = SimpleNamespace(
        timestamp=now - 60, task_type="fix_bug", test_delta={"before": ran, "after": ran}
    )
    assert _stale_task_type([tested, tested], now) == "fix_bug"


def test_react_answer_parsing_survives_an_empty_reply():
    """An empty forced final answer ended the cycle with an unhandled
    JSONDecodeError (2026-09-25 00:29); it is now 'nothing identified'."""
    from types import SimpleNamespace
    from ouroboros.improvement import _parse_react_answer, _tool_args

    assert _parse_react_answer("") is None
    assert _parse_react_answer(None) is None
    assert _parse_react_answer('```json\n{"task_type": "add_test"}\n```') == {"task_type": "add_test"}
    call = lambda args: SimpleNamespace(function=SimpleNamespace(name="read_file", arguments=args))
    assert _tool_args(call("")) == {}
    assert _tool_args(call("{broken")) == {}
    assert _tool_args(call('{"path": "a.py"}')) == {"path": "a.py"}
