"""Tests for community-assisted self-improvement engine."""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from ouroboros.community_improvement import (
    MAX_COMMUNITY_HISTORY,
    clear_community_improvement,
    step_community_improvement,
    _step_identify,
    _step_post,
    _step_wait,
    _step_analyze,
    _step_implement,
)
from ouroboros.config import SafetyConfig
from ouroboros.improvement import CodeChange, ImprovementResult, ImprovementTask
from ouroboros.moltbook import Credentials, RunnerConfig
from ouroboros.test_runner import TestResult


def _make_cfg(**overrides) -> RunnerConfig:
    defaults = {
        "enable_community_improvement": True,
        "community_wait_hours": 48,
        "community_min_comments_for_early": 3,
        "community_improvement_interval_hours": 72,
        "improvement_model": "gpt-4o",
        "dry_run": False,
        "default_submolt": "general",
    }
    defaults.update(overrides)
    return RunnerConfig(**defaults)


def _make_creds() -> Credentials:
    return Credentials(api_key="test-key", agent_name="test-agent")


def _make_state(**overrides) -> dict:
    state = {
        "community_improvement": None,
        "community_improvement_history": [],
        "last_community_improvement_start": None,
    }
    state.update(overrides)
    return state


def _make_ci_state(status="identified", **overrides) -> dict:
    ci = {
        "status": status,
        "task_id": "abc12345",
        "task_type": "fix_test",
        "description": "Fix failing test_foo",
        "target_files": ["tests/test_foo.py"],
        "evidence": "test_foo fails with AssertionError",
        "code_context": {"tests/test_foo.py": "def test_foo(): assert False"},
        "test_output": "1 failed",
        "post_id": None,
        "posted_at": None,
        "wait_until": None,
        "comments_snapshot": [],
        "selected_comment": None,
        "fallback_used": False,
        "pr_url": None,
    }
    ci.update(overrides)
    return ci


# -- test_step_identify_creates_state --

@patch("ouroboros.community_improvement.get_repo_root")
@patch("ouroboros.community_improvement.git_ops")
@patch("ouroboros.community_improvement.get_codebase_summary")
@patch("ouroboros.community_improvement.run_tests")
@patch("ouroboros.community_improvement.load_history")
@patch("ouroboros.community_improvement.llm")
def test_step_identify_creates_state(
    mock_llm, mock_history, mock_tests, mock_summary, mock_git, mock_root,
):
    mock_root.return_value = Path("/fake/repo")
    mock_git.has_open_improvement_prs.return_value = False
    mock_summary.return_value = "codebase summary"
    mock_tests.return_value = TestResult(passed=5, failed=1, errors=0, returncode=1)
    mock_history.return_value = []
    mock_llm.analyze_codebase.return_value = {
        "task_type": "fix_test",
        "description": "Fix test_foo",
        "target_files": ["tests/test_foo.py"],
        "evidence": "AssertionError in test_foo",
    }

    state = _make_state()
    cfg = _make_cfg()
    safety = SafetyConfig()

    result = step_community_improvement(
        MagicMock(), state, _make_creds(), cfg, safety,
    )

    assert result == "identified"
    ci = state["community_improvement"]
    assert ci is not None
    assert ci["status"] == "identified"
    assert ci["task_type"] == "fix_test"
    assert ci["description"] == "Fix test_foo"
    assert ci["target_files"] == ["tests/test_foo.py"]
    assert state["last_community_improvement_start"] is not None


# -- test_step_post_creates_moltbook_post --

@patch("ouroboros.community_improvement.moltbook")
@patch("ouroboros.community_improvement.llm")
def test_step_post_creates_moltbook_post(mock_llm, mock_moltbook):
    mock_llm.generate_question_post.return_value = {
        "title": "Why does test_foo fail?",
        "content": "## Problem\ntest_foo fails...",
    }
    mock_moltbook.create_post.return_value = {"id": "post-123"}

    state = _make_state(community_improvement=_make_ci_state(status="identified"))
    cfg = _make_cfg()

    result = _step_post(MagicMock(), state, _make_creds(), cfg)

    assert result == "posted"
    ci = state["community_improvement"]
    assert ci["post_id"] == "post-123"
    assert ci["status"] == "waiting"
    assert ci["posted_at"] is not None
    assert ci["wait_until"] is not None
    assert ci["wait_until"] > ci["posted_at"]


# -- test_step_wait_stays_waiting --

@patch("ouroboros.community_improvement.moltbook")
def test_step_wait_stays_waiting(mock_moltbook):
    mock_moltbook.get_post_comments.return_value = {"comments": []}

    now = int(time.time())
    ci = _make_ci_state(
        status="waiting",
        post_id="post-123",
        posted_at=now - 3600,  # 1 hour ago
        wait_until=now + 3600 * 47,  # 47 hours from now
    )
    state = _make_state(community_improvement=ci)
    cfg = _make_cfg()

    result = _step_wait(state, _make_creds(), cfg)

    assert result == "waiting"
    assert state["community_improvement"]["status"] == "waiting"


# -- test_step_wait_advances_on_deadline --

@patch("ouroboros.community_improvement.moltbook")
def test_step_wait_advances_on_deadline(mock_moltbook):
    mock_moltbook.get_post_comments.return_value = {
        "comments": [{"id": "c1", "content": "try X", "author": {"name": "alice"}}],
    }

    now = int(time.time())
    ci = _make_ci_state(
        status="waiting",
        post_id="post-123",
        posted_at=now - 3600 * 49,  # 49 hours ago
        wait_until=now - 3600,  # 1 hour ago -- expired
    )
    state = _make_state(community_improvement=ci)
    cfg = _make_cfg()

    result = _step_wait(state, _make_creds(), cfg)

    assert result == "deadline_analysis"
    assert state["community_improvement"]["status"] == "analyzing"


# -- test_step_wait_early_with_enough_comments --

@patch("ouroboros.community_improvement.moltbook")
def test_step_wait_early_with_enough_comments(mock_moltbook):
    comments = [
        {"id": f"c{i}", "content": f"suggestion {i}", "author": {"name": f"user{i}"}}
        for i in range(3)
    ]
    mock_moltbook.get_post_comments.return_value = {"comments": comments}

    now = int(time.time())
    ci = _make_ci_state(
        status="waiting",
        post_id="post-123",
        posted_at=now - 3600,  # 1 hour ago
        wait_until=now + 3600 * 47,  # still far away
    )
    state = _make_state(community_improvement=ci)
    cfg = _make_cfg(community_min_comments_for_early=3)

    result = _step_wait(state, _make_creds(), cfg)

    assert result == "early_analysis"
    assert state["community_improvement"]["status"] == "analyzing"


# -- test_step_analyze_selects_best --

@patch("ouroboros.community_improvement.llm")
def test_step_analyze_selects_best(mock_llm):
    mock_llm.analyze_code_suggestions.return_value = {
        "has_actionable": True,
        "suggestions": [
            {
                "author": "bob",
                "comment_id": "c2",
                "approach": "use a mock instead",
                "code_snippets": ["mock.patch(...)"],
                "target_files": ["tests/test_foo.py"],
                "confidence": 0.6,
            },
            {
                "author": "alice",
                "comment_id": "c1",
                "approach": "fix the assertion",
                "code_snippets": ["assert result == expected"],
                "target_files": ["tests/test_foo.py"],
                "confidence": 0.9,
            },
        ],
    }

    comments = [
        {"id": "c1", "content": "fix the assertion", "author": {"name": "alice"}},
        {"id": "c2", "content": "use a mock", "author": {"name": "bob"}},
    ]
    ci = _make_ci_state(status="analyzing", comments_snapshot=comments)
    state = _make_state(community_improvement=ci)
    cfg = _make_cfg()

    result = _step_analyze(MagicMock(), state, cfg)

    assert result == "suggestion_selected"
    assert state["community_improvement"]["status"] == "implementing"
    selected = state["community_improvement"]["selected_comment"]
    assert selected["author"] == "alice"  # highest confidence
    assert selected["confidence"] == 0.9


# -- test_step_analyze_fallback_no_comments --

@patch("ouroboros.community_improvement.llm")
def test_step_analyze_fallback_no_comments(mock_llm):
    ci = _make_ci_state(status="analyzing", comments_snapshot=[])
    state = _make_state(community_improvement=ci)
    cfg = _make_cfg()

    result = _step_analyze(MagicMock(), state, cfg)

    assert result == "fallback_no_comments"
    assert state["community_improvement"]["status"] == "fallback"
    assert state["community_improvement"]["fallback_used"] is True


# -- test_step_implement_creates_pr_with_credit --

@patch("ouroboros.community_improvement.get_repo_root")
@patch("ouroboros.community_improvement.read_file_raw")
@patch("ouroboros.community_improvement.validate_improvement")
@patch("ouroboros.community_improvement.git_ops")
@patch("ouroboros.community_improvement.llm")
@patch("ouroboros.community_improvement.moltbook")
def test_step_implement_creates_pr_with_credit(
    mock_moltbook, mock_llm, mock_git, mock_validate, mock_read, mock_root,
):
    mock_root.return_value = Path("/fake/repo")
    mock_read.return_value = "original content"
    mock_llm.plan_code_change.return_value = "Step 1: fix assertion"
    mock_llm.generate_code_from_suggestion.return_value = [
        {"file_path": "tests/test_foo.py", "new_content": "fixed content", "description": "fixed assertion"},
    ]
    mock_validate.return_value = ImprovementResult(
        task=ImprovementTask("abc", "fix_test", "fix test_foo", ["tests/test_foo.py"], "fails"),
        status="success",
        test_before=TestResult(passed=5, failed=1, errors=0, returncode=1),
        test_after=TestResult(passed=6, failed=0, errors=0, returncode=0),
    )
    mock_git.current_branch.return_value = "main"
    mock_git.make_branch_name.return_value = "ouroboros/improve-community-fix_test-123"
    mock_git.create_pr.return_value = "https://github.com/repo/pull/42"
    mock_moltbook._post_url.return_value = "https://moltbook.com/post/post-123"

    ci = _make_ci_state(
        status="implementing",
        post_id="post-123",
        selected_comment={
            "author": "alice",
            "content": "fix the assertion to match expected",
            "comment_id": "c1",
            "code_snippets": [],
            "confidence": 0.9,
        },
    )
    state = _make_state(community_improvement=ci)
    cfg = _make_cfg()
    safety = SafetyConfig()

    result = _step_implement(MagicMock(), state, _make_creds(), cfg, safety)

    assert result == "pr_created"
    assert state["community_improvement"]["pr_url"] == "https://github.com/repo/pull/42"
    assert state["community_improvement"]["status"] == "completed"

    # Verify PR body contains commenter credit
    pr_body = mock_git.create_pr.call_args.kwargs.get("body") or mock_git.create_pr.call_args[1].get("body", "")
    if not pr_body:
        # positional args
        call_args = mock_git.create_pr.call_args
        pr_body = call_args[1].get("body", "") if call_args[1] else ""
    assert "alice" in pr_body


# -- test_step_implement_reverts_on_regression --

@patch("ouroboros.community_improvement.get_repo_root")
@patch("ouroboros.community_improvement.read_file_raw")
@patch("ouroboros.community_improvement.validate_improvement")
@patch("ouroboros.community_improvement.llm")
def test_step_implement_reverts_on_regression(mock_llm, mock_validate, mock_read, mock_root):
    mock_root.return_value = Path("/fake/repo")
    mock_read.return_value = "original"
    mock_llm.plan_code_change.return_value = "Step 1: try fix"
    mock_llm.generate_code_from_suggestion.return_value = [
        {"file_path": "tests/test_foo.py", "new_content": "bad fix", "description": "broken"},
    ]
    mock_validate.return_value = ImprovementResult(
        task=ImprovementTask("abc", "fix_test", "fix", ["tests/test_foo.py"], "fails"),
        status="reverted",
        test_before=TestResult(passed=5, failed=1, errors=0, returncode=1),
        test_after=TestResult(passed=3, failed=3, errors=0, returncode=1),
    )

    ci = _make_ci_state(
        status="implementing",
        selected_comment={"author": "bob", "content": "try X", "comment_id": "c1"},
    )
    state = _make_state(community_improvement=ci)
    cfg = _make_cfg()
    safety = SafetyConfig()

    result = _step_implement(MagicMock(), state, _make_creds(), cfg, safety)

    assert result == "validation_reverted"
    assert state["community_improvement"]["status"] == "failed"


# -- test_clear_archives_to_history --

def test_clear_archives_to_history():
    ci = _make_ci_state(
        status="completed",
        post_id="post-123",
        pr_url="https://github.com/repo/pull/42",
        selected_comment={"author": "alice", "content": "fix it"},
    )
    state = _make_state(community_improvement=ci)

    clear_community_improvement(state)

    assert state["community_improvement"] is None
    history = state["community_improvement_history"]
    assert len(history) == 1
    assert history[0]["task_id"] == "abc12345"
    assert history[0]["pr_url"] == "https://github.com/repo/pull/42"
    assert history[0]["selected_author"] == "alice"
    assert history[0]["archived_at"] is not None


def test_clear_caps_history():
    state = _make_state(
        community_improvement=_make_ci_state(status="completed"),
        community_improvement_history=[
            {"task_id": f"old-{i}", "archived_at": i}
            for i in range(MAX_COMMUNITY_HISTORY)
        ],
    )

    clear_community_improvement(state)

    assert len(state["community_improvement_history"]) == MAX_COMMUNITY_HISTORY
    # The oldest entry should have been dropped
    assert state["community_improvement_history"][0]["task_id"] == "old-1"
    assert state["community_improvement_history"][-1]["task_id"] == "abc12345"
