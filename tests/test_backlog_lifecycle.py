"""The backlog has to close its own items.

`mark_done` and `mark_failed` had zero production callers. An item was injected
into the identify prompt as HIGH-PRIORITY and then never resolved, so the same
head came back every cycle forever. That is why "code-aware indexing in
MemoryStore" was implemented on 2026-06-27 and again on 2026-08-22 -- the second
cycle re-implemented an existing method and moved the test count 966 -> 966.
"""

import pytest

from ouroboros import backlog, improvement


class _Task:
    def __init__(self, description):
        self.description = description
        self.task_id = "t1"
        self.task_type = "fix_bug"


class _Result:
    def __init__(self, status):
        self.status = status
        self.details = ""
        self.changes = []


@pytest.fixture
def repo(tmp_path):
    return tmp_path


# ------------------------------------------------------------ content_overlap

class TestContentOverlap:
    def test_identical_descriptions_score_one(self):
        d = "Fix summary line parsing in _parse_pytest_output"
        assert backlog.content_overlap(d, d) == 1.0

    def test_a_restatement_clears_the_threshold(self):
        """The real duplicate shape: same work, reworded."""
        assert backlog.content_overlap(
            "Fix summary line parsing in _parse_pytest_output",
            "Fix the summary line parsing in _parse_pytest_output within test_runner",
        ) >= 0.8

    def test_neighbouring_work_does_not(self):
        """The false-positive that would silently drop real work."""
        assert backlog.content_overlap(
            "Add unit tests for memory module",
            "Add unit tests for backlog module",
        ) < 0.8

    def test_unrelated_work_scores_zero(self):
        assert backlog.content_overlap(
            "Fix parsing in _parse_pytest_output",
            "Add stopword filtering to encode_text",
        ) == 0.0

    def test_empty_input_is_not_a_match(self):
        assert backlog.content_overlap("", "anything at all") == 0.0

    def test_filler_words_alone_do_not_match(self):
        assert backlog.content_overlap("the and to of", "the and to of") == 0.0


# --------------------------------------------------------------- the epilogue

class TestFinalizeBacklog:
    def _ctx(self, repo, item, task_desc, status):
        return {"repo_root": repo, "backlog_item": item,
                "task": _Task(task_desc), "result": _Result(status)}

    def test_success_marks_the_item_done(self, repo):
        item = backlog.add_item(repo, "fix_bug", "Fix parsing in _parse_pytest_output", priority=9)
        improvement._finalize_backlog(
            self._ctx(repo, item, "Fix parsing in _parse_pytest_output", "success"))
        assert backlog.load_backlog(repo)[0]["status"] == "done"

    def test_failure_counts_an_attempt(self, repo):
        item = backlog.add_item(repo, "fix_bug", "Fix parsing in _parse_pytest_output", priority=9)
        improvement._finalize_backlog(
            self._ctx(repo, item, "Fix parsing in _parse_pytest_output", "failed"))
        stored = backlog.load_backlog(repo)[0]
        assert stored["attempts"] == 1
        assert stored["status"] == "pending", "one failure is not abandonment"

    def test_three_failures_abandon_it(self, repo):
        item = backlog.add_item(repo, "fix_bug", "Fix parsing in _parse_pytest_output", priority=9)
        for _ in range(3):
            improvement._finalize_backlog(
                self._ctx(repo, item, "Fix parsing in _parse_pytest_output", "failed"))
        stored = backlog.load_backlog(repo)[0]
        assert stored["attempts"] == 3
        assert stored["status"] == "abandoned"
        assert backlog.get_pending(repo) == [], "an abandoned item stops being offered"

    @pytest.mark.parametrize("status", ["skipped", "pending"])
    def test_a_non_attempt_does_not_count_as_one(self, repo, status):
        """`skipped` is a dry run; `pending` is the default still in place when an
        exception escapes after the result exists. Neither is an attempt.

        Counting them meant three dry-run preflights -- or three cycles during a
        backend outage -- would abandon a live item without anyone touching it.
        """
        item = backlog.add_item(repo, "fix_bug", "Fix parsing in _parse_pytest_output", priority=9)
        for _ in range(3):
            improvement._finalize_backlog(
                self._ctx(repo, item, "Fix parsing in _parse_pytest_output", status))
        stored = backlog.load_backlog(repo)[0]
        assert stored["attempts"] == 0, f"{status} must not count as an attempt"
        assert stored["status"] == "pending"
        assert backlog.get_pending(repo), "the item must still be offered"

    def test_a_cycle_that_did_something_else_resolves_nothing(self, repo):
        """The failure mode worth protecting against. Marking the wrong item
        done silently drops real work off the agenda."""
        item = backlog.add_item(repo, "fix_bug", "Fix parsing in _parse_pytest_output", priority=9)
        improvement._finalize_backlog(
            self._ctx(repo, item, "Add stopword filtering to encode_text", "success"))
        stored = backlog.load_backlog(repo)[0]
        assert stored["status"] == "pending"
        assert stored["attempts"] == 0

    def test_no_item_offered_is_a_no_op(self, repo):
        backlog.add_item(repo, "fix_bug", "Something else", priority=9)
        improvement._finalize_backlog(
            {"repo_root": repo, "task": _Task("x"), "result": _Result("success")})
        assert backlog.load_backlog(repo)[0]["status"] == "pending"

    def test_a_skipped_cycle_is_a_no_op(self, repo):
        item = backlog.add_item(repo, "fix_bug", "Fix parsing", priority=9)
        improvement._finalize_backlog({"repo_root": repo, "backlog_item": item})
        assert backlog.load_backlog(repo)[0]["status"] == "pending"

    def test_it_never_raises(self, repo):
        """Runs from a `finally`; a bookkeeping failure must not crash a cycle."""
        improvement._finalize_backlog(
            {"repo_root": repo, "backlog_item": {"id": None, "description": "x"},
             "task": _Task("x"), "result": _Result("success")})


class TestTheRepeatItActuallyFixes:
    def test_a_completed_item_stops_being_offered(self, repo):
        """End to end: the exact 2026-06-27 / 2026-08-22 repeat."""
        desc = "Implement code-aware indexing in MemoryStore.index_file"
        item = backlog.add_item(repo, "add_feature", desc, priority=9)
        assert backlog.get_pending(repo), "offered before"

        improvement._finalize_backlog({
            "repo_root": repo, "backlog_item": item,
            "task": _Task(desc), "result": _Result("success")})

        assert backlog.get_pending(repo) == [], "must not be offered again"
