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
    _is_path_allowed,
    _validate_changes,
    _count_changed_lines,
    apply_changes,
    revert_changes,
    validate_improvement,
)
from ouroboros.test_runner import TestResult


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


@patch("ouroboros.improvement.run_tests")
def test_validate_improvement_success(mock_run_tests):
    mock_run_tests.side_effect = [
        TestResult(passed=5, failed=0, errors=0, returncode=0),  # before
        TestResult(passed=6, failed=0, errors=0, returncode=0),  # after
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
        TestResult(passed=5, failed=0, errors=0, returncode=0),  # before
        TestResult(passed=3, failed=2, errors=0, returncode=1),  # after - regression!
    ]

    task = ImprovementTask("abc", "fix_bug", "fix it", ["src/ouroboros/x.py"], "broken")
    changes = [
        CodeChange("src/ouroboros/x.py", "old", "new", "fix"),
    ]

    with patch("ouroboros.improvement.apply_changes"):
        result = validate_improvement(task, changes, Path("/tmp/repo"))

    assert result.status == "reverted"
    mock_revert.assert_called_once()
