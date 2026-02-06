"""Tests for policies module."""

from ouroboros.config import SafetyConfig
from ouroboros.policies import (
    PolicyError,
    require_pr_only,
    validate_modification_scope,
    validate_change_size,
)


def test_require_pr_only_passes():
    require_pr_only(True)  # should not raise


def test_require_pr_only_fails():
    try:
        require_pr_only(False)
        assert False, "Should have raised PolicyError"
    except PolicyError:
        pass


def test_validate_modification_scope_allowed():
    violations = validate_modification_scope(["src/ouroboros/llm.py", "tests/test_foo.py"])
    assert violations == []


def test_validate_modification_scope_forbidden():
    violations = validate_modification_scope(["src/ouroboros/config.py"])
    assert len(violations) == 1
    assert "Forbidden" in violations[0]


def test_validate_modification_scope_out_of_scope():
    violations = validate_modification_scope(["README.md"])
    assert len(violations) == 1
    assert "Out of scope" in violations[0]


def test_validate_modification_scope_mixed():
    violations = validate_modification_scope([
        "src/ouroboros/llm.py",  # ok
        "src/ouroboros/config.py",  # forbidden
        "setup.py",  # out of scope
    ])
    assert len(violations) == 2


def test_validate_change_size_ok():
    violations = validate_change_size(2, 100)
    assert violations == []


def test_validate_change_size_too_many_files():
    violations = validate_change_size(5, 100)
    assert len(violations) == 1
    assert "files" in violations[0].lower()


def test_validate_change_size_too_many_lines():
    violations = validate_change_size(1, 500)
    assert len(violations) == 1
    assert "lines" in violations[0].lower()


def test_validate_change_size_both_exceeded():
    violations = validate_change_size(5, 500)
    assert len(violations) == 2


def test_config_defaults_are_safe():
    config = SafetyConfig()
    assert config.pr_only is True
    assert config.allow_write_default_branch is False
    assert config.require_human_approval is True
    assert config.max_improvements_per_day == 3
    assert config.max_changed_files_per_pr == 3
    assert config.max_lines_changed_per_pr == 200
    assert "config.py" in config.forbidden_modification_paths
    assert "improvement.py" in config.forbidden_modification_paths
