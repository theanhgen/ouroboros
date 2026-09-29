"""Tests for the prompt-context builders extracted from improvement.py (#362)."""

from ouroboros import improvement
from ouroboros.context_helpers import _build_failed_attempts_context
from ouroboros.evaluation import EvaluationRecord


def test_failed_attempts_context_lives_in_context_helpers():
    # improvement.py keeps calling the helper, but it is defined in one place.
    assert improvement._build_failed_attempts_context is _build_failed_attempts_context
    assert _build_failed_attempts_context.__module__ == "ouroboros.context_helpers"


def test_failed_attempts_context_keeps_last_entries_and_truncates_feedback():
    history = [
        EvaluationRecord(task_id=str(i), task_type="fix_bug",
                         description=f"attempt {i}", outcome="reverted")
        for i in range(7)
    ]
    history[-1].feedback = "x" * 200

    context = _build_failed_attempts_context(history, max_entries=5)

    assert context.startswith("### Previously Failed Attempts (DO NOT repeat these)")
    assert "attempt 1" not in context
    assert "attempt 2" in context and "attempt 6" in context
    assert "x" * 120 in context and "x" * 121 not in context
    assert _build_failed_attempts_context([]) == ""
