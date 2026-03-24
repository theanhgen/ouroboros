"""Tests for improvement engine."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from ouroboros.config import SafetyConfig
from ouroboros.improvement import (
    ImprovementTask,
    CodeChange,
    ImprovementResult,
    IMMUTABLE_FILES,
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
from ouroboros.test_runner import RunnerOutcome


def test_immutable_files():
    assert "config.py" in IMMUTABLE_FILES
    assert "improvement.py" in IMMUTABLE_FILES
    assert "git_ops.py" in IMMUTABLE_FILES
    assert "evaluation.py" in IMMUTABLE_FILES
    assert "policies.py" in IMMUTABLE_FILES


def test_is_path_allowed():
    config = SafetyConfig()
    assert _is_path_allowed("src/ouroboros/llm.py", config) is True
    assert _is_path_allowed("tests/test_foo.py", config) is True
    assert _is_path_allowed("src/ouroboros/config.py", config) is False
    assert _is_path_allowed("src/ouroboros/improvement.py", config) is False
    assert _is_path_allowed("README.md", config) is False
    assert _is_path_allowed("setup.py", config) is False


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


def test_count_changed_lines():
    assert _count_changed_lines("a\nb\nc\n", "a\nb\nc\n") == 0
    assert _count_changed_lines("a\nb\n", "a\nX\n") == 1
    assert _count_changed_lines("a\n", "a\nb\n") == 1


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
        CodeChange("src/ouroboros/foo.py", "original", "modified", "test"),
    ]
    revert_changes(changes, tmp_path)

    assert target.read_text() == "original"


def test_revert_new_file(tmp_path):
    src_dir = tmp_path / "src" / "ouroboros"
    src_dir.mkdir(parents=True)
    target = src_dir / "new_file.py"
    target.write_text("new content")

    changes = [
        CodeChange("src/ouroboros/new_file.py", "", "new content", "new file"),
    ]
    revert_changes(changes, tmp_path)
    assert not target.exists()  # empty original means file was new, so remove it


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

    result = run_improvement_cycle(client=mock_client, state={}, config=SafetyConfig(), model="gpt-4o")

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
@patch("ouroboros.improvement.revert_changes")
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
