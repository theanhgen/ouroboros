"""A review that could not be read is not a review that objected.

18 of the recorded rejections were "Reviewer rejection: ..."; 5 of those were
actually "Reviewer failed to provide structured feedback" -- a parse failure
reported as the reviewer judging the change and finding a defect. The verdict
stays fail-closed (an unreadable review must never merge code); only the reason
changes, so the log distinguishes "objected" from "never answered".
"""

import json

import pytest

from ouroboros import llm


class _Client:
    """Duck-types the OpenAI client; returns whatever text it is given."""

    def __init__(self, text=None, exc=None):
        self._text, self._exc = text, exc
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **_kw):
        if self._exc:
            raise self._exc
        msg = type("M", (), {"content": self._text})()
        choice = type("C", (), {"message": msg})()
        return type("R", (), {"choices": [choice], "usage": None})()


TASK = {"description": "fix a bug"}
CHANGES = [{"file_path": "a.py", "description": "d", "new_content": "x = 1"}]


def _review(client):
    return llm.review_code_changes(client, TASK, CHANGES, model="m")


class TestGenuineVerdicts:
    def test_approval_is_passed_through(self):
        ok, feedback, _ = _review(_Client(json.dumps({"approved": True, "feedback": "fine"})))
        assert ok is True
        assert feedback == "fine"

    def test_a_real_rejection_is_passed_through(self):
        ok, feedback, _ = _review(
            _Client(json.dumps({"approved": False, "feedback": "drops the None check"}))
        )
        assert ok is False
        assert "drops the None check" in feedback
        assert "not a rejection" not in feedback

    def test_json_wrapped_in_prose_still_parses(self):
        ok, _, _ = _review(_Client('Sure!\n{"approved": true, "feedback": "ok"}\nDone.'))
        assert ok is True


class TestFailuresAreNotRejections:
    def test_a_failed_call_says_so(self):
        ok, feedback, _ = _review(_Client(exc=RuntimeError("agy exited 1: permission denied")))
        assert ok is False, "must still fail closed"
        assert "not a rejection" in feedback
        assert "permission denied" in feedback

    def test_empty_output_says_so(self):
        ok, feedback, _ = _review(_Client(""))
        assert ok is False
        assert "not a rejection" in feedback
        assert "no output" in feedback

    def test_unparseable_output_says_so_and_shows_it(self):
        ok, feedback, _ = _review(_Client("I think this looks reasonable to me."))
        assert ok is False
        assert "not a rejection" in feedback
        assert "reasonable" in feedback, "the actual output should be quoted for diagnosis"

    def test_every_failure_mode_still_fails_closed(self):
        """Safety posture is unchanged -- only the reason improves."""
        for client in (_Client(exc=RuntimeError("boom")), _Client(""), _Client("prose")):
            ok, _, _ = _review(client)
            assert ok is False

    def test_the_three_failure_reasons_are_distinct(self):
        reasons = {
            _review(_Client(exc=RuntimeError("boom")))[1],
            _review(_Client(""))[1],
            _review(_Client("prose"))[1],
        }
        assert len(reasons) == 3, "a caller must be able to tell them apart"

    def test_a_failure_is_never_confusable_with_a_verdict(self):
        _, failure, _ = _review(_Client("prose"))
        _, verdict, _ = _review(
            _Client(json.dumps({"approved": False, "feedback": "real defect"}))
        )
        assert "not a rejection" in failure
        assert "not a rejection" not in verdict
