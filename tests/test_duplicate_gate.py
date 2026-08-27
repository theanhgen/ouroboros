"""Do not do the same work twice -- and do not refuse to do new work.

Of the two failure modes this gate sits between, the second is much worse.
Duplicating a task wastes one cycle and is visible in the history. Falsely
suppressing a valid task drops it silently and forever, and looks exactly like
the agent choosing not to do it.

So the gate is narrow by construction: exact task id, or >= 0.8 Jaccard over
content words against SUCCEEDED history. Broad full-text matching is forbidden
here -- two unrelated tasks sharing "test", "parser" and "memory" score highly
under FTS and would trip it.
"""

import pytest

from ouroboros.improvement import _already_completed, _DUPLICATE_THRESHOLD


class _Rec:
    def __init__(self, description, outcome="merged", task_id=None, task_type="fix_bug"):
        self.description = description
        self.outcome = outcome
        self.task_id = task_id
        self.task_type = task_type
        self.feedback = ""


class _Task:
    def __init__(self, description, task_id=None):
        self.description = description
        self.task_id = task_id
        self.task_type = "fix_bug"


# The real repeat, from production history.
CODE_INDEXING = (
    "Implement code-aware indexing in MemoryStore.index_file to parse Python "
    "files and extract functions and classes as facts"
)


class TestItCatchesRealDuplicates:
    def test_the_2026_08_22_repeat(self):
        """Implemented 2026-06-27, proposed and implemented again 2026-08-22,
        moving the test count 966 -> 966."""
        history = [_Rec(CODE_INDEXING)]
        assert _already_completed(_Task(CODE_INDEXING), history) is not None

    def test_a_reworded_restatement(self):
        history = [_Rec("Fix summary line parsing in _parse_pytest_output within test_runner")]
        task = _Task("Fix the summary line parsing in _parse_pytest_output in src/test_runner")
        assert _already_completed(task, history) is not None

    def test_an_exact_task_id_match(self):
        history = [_Rec("something worded entirely differently", task_id="abc123")]
        assert _already_completed(_Task("unrelated wording", task_id="abc123"), history) is not None

    def test_it_returns_what_was_matched(self):
        history = [_Rec(CODE_INDEXING)]
        assert _already_completed(_Task(CODE_INDEXING), history) == CODE_INDEXING


class TestItDoesNotSuppressValidWork:
    """The zero-false-positive set. Every one of these shares heavy vocabulary
    with completed work and every one is genuinely different work."""

    HISTORY = [
        _Rec("Add unit tests for the memory module in tests/test_memory.py"),
        _Rec("Fix summary line parsing in _parse_pytest_output within test_runner"),
        _Rec("Implement code-aware indexing in MemoryStore.index_file"),
        _Rec("Add stopword filtering to encode_text in holographic.py"),
    ]

    @pytest.mark.parametrize("description", [
        "Add unit tests for the backlog module in tests/test_backlog.py",
        "Add unit tests for the policies module in tests/test_policies.py",
        "Fix summary line parsing in _parse_coverage_output within test_runner",
        "Fix error line parsing in _parse_pytest_output within test_runner",
        "Implement code-aware indexing in IndexManager.run_hygiene",
        "Remove code-aware indexing from MemoryStore.index_file",
        "Add stopword filtering to search_facts in memory.py",
        "Add caching to encode_text in holographic.py",
        "Document the memory module's indexing behaviour in docs/",
        "Add integration tests for the memory and backlog modules together",
    ])
    def test_valid_near_token_work_is_not_suppressed(self, description):
        assert _already_completed(_Task(description), self.HISTORY) is None, (
            f"falsely suppressed: {description}")


class TestScope:
    def test_a_failed_attempt_does_not_suppress_a_retry(self):
        """Failure is not completion. Discouraging a retry is the job of the
        'previously failed' context, which advises rather than overrides."""
        for outcome in ("failed", "reverted", "closed"):
            history = [_Rec(CODE_INDEXING, outcome=outcome)]
            assert _already_completed(_Task(CODE_INDEXING), history) is None, outcome

    def test_an_empty_description_matches_nothing(self):
        assert _already_completed(_Task(""), [_Rec("anything")]) is None
        assert _already_completed(_Task("   "), [_Rec("anything")]) is None

    def test_empty_history_matches_nothing(self):
        assert _already_completed(_Task(CODE_INDEXING), []) is None

    def test_a_record_with_no_description_is_survivable(self):
        assert _already_completed(_Task(CODE_INDEXING), [_Rec(None)]) is None

    def test_the_threshold_is_where_it_claims_to_be(self):
        assert _DUPLICATE_THRESHOLD == 0.8
