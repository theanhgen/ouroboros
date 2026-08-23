"""A failed API call must not be recorded as "the model returned nothing".

Every improvement attempt in August 2026 was logged as "no plan generated".
`chat_completion` catches every exception and returns "", `plan_code_change`
turns "" into None, and the cycle records the same message either way -- so
three weeks of a possibly-broken API read as three weeks of an unproductive
agent. These tests keep the two distinguishable.
"""

import pytest

from ouroboros import llm


class _Boom:
    """A client whose every call raises, like an API rejecting the model id."""

    def __init__(self, exc=None):
        self.exc = exc or RuntimeError("model `gpt-x` has been retired")
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **_kwargs):
        raise self.exc


class _Silent:
    """A client that succeeds but returns empty content."""

    class _Msg:
        content = ""

    class _Choice:
        message = None

    def __init__(self):
        self.chat = self
        choice = self._Choice()
        choice.message = self._Msg()
        self._resp = type("R", (), {"choices": [choice], "usage": None})()

    @property
    def completions(self):
        return self

    def create(self, **_kwargs):
        return self._resp


# ------------------------------------------------------------ chat_completion


class TestChatCompletionAttribution:
    def test_a_raised_call_reports_the_exception(self):
        seen = []
        content, usage = llm.chat_completion(
            _Boom(), "sys", "user", model="gpt-test", on_error=seen.append
        )
        assert content == ""
        assert usage is None
        assert len(seen) == 1
        assert "RuntimeError" in seen[0]
        assert "retired" in seen[0]

    def test_an_empty_success_reports_nothing(self):
        """The distinction that was missing: this one really is 'no content'."""
        seen = []
        content, _ = llm.chat_completion(
            _Silent(), "sys", "user", model="gpt-test", on_error=seen.append
        )
        assert content == ""
        assert seen == [], "an empty response is not an error"

    def test_the_callback_is_optional(self):
        """Every existing caller omits it and must keep working unchanged."""
        content, usage = llm.chat_completion(_Boom(), "sys", "user", model="gpt-test")
        assert (content, usage) == ("", None)

    def test_the_return_shape_is_unchanged(self):
        """Callers unpack a 2-tuple; adding a third element would break them all."""
        result = llm.chat_completion(_Boom(), "s", "u", model="m", on_error=lambda _: None)
        assert isinstance(result, tuple) and len(result) == 2

    def test_the_message_carries_type_and_text(self):
        seen = []
        llm.chat_completion(
            _Boom(ValueError("bad request: unknown parameter")),
            "s", "u", model="m", on_error=seen.append,
        )
        assert seen[0].startswith("ValueError: ")
        assert "unknown parameter" in seen[0]


# ----------------------------------------------------------- plan_code_change


class TestPlanAttribution:
    def test_a_failed_planning_call_surfaces_the_cause(self):
        seen = []
        plan, usage = llm.plan_code_change(
            _Boom(), {"task_type": "fix_bug", "description": "d"}, "code",
            model="gpt-test", on_error=seen.append,
        )
        assert plan is None
        assert seen and "RuntimeError" in seen[0]

    def test_an_empty_plan_is_not_an_error(self):
        seen = []
        plan, _ = llm.plan_code_change(
            _Silent(), {"task_type": "fix_bug", "description": "d"}, "code",
            model="gpt-test", on_error=seen.append,
        )
        assert plan is None
        assert seen == []

    def test_both_paths_still_return_none_for_the_plan(self):
        """The caller's `if not plan` check is unchanged -- this fix adds a
        reason, it does not alter control flow."""
        for client in (_Boom(), _Silent()):
            plan, _ = llm.plan_code_change(
                client, {"task_type": "t", "description": "d"}, "c", model="m"
            )
            assert plan is None


class TestGenerateAttribution:
    def test_a_failed_generation_call_surfaces_the_cause(self):
        seen = []
        changes, _ = llm.generate_code(
            _Boom(), "plan", {"a.py": "x"}, "constraints",
            model="gpt-test", on_error=seen.append,
        )
        assert changes is None
        assert seen and "RuntimeError" in seen[0]

    def test_generate_callback_is_optional(self):
        changes, _ = llm.generate_code(_Boom(), "p", {"a.py": "x"}, "c", model="m")
        assert changes is None


# -------------------------------------------------- what the record now says


class TestRecordedReason:
    """The point of the whole change: the learning log tells you which it was."""

    def _details_for(self, errors):
        # Mirrors the branch in run_improvement_cycle.
        return (
            f"Planning call failed: {errors[0]}" if errors
            else "Failed to generate a concrete implementation plan (model returned nothing)"
        )

    def test_an_api_failure_names_the_exception(self):
        details = self._details_for(["RuntimeError: model `gpt-x` has been retired"])
        assert "Planning call failed" in details
        assert "retired" in details

    def test_an_empty_response_says_so_explicitly(self):
        details = self._details_for([])
        assert "model returned nothing" in details

    def test_the_two_are_not_the_same_string(self):
        """This is the regression that cost three weeks."""
        assert self._details_for(["APIError: 404"]) != self._details_for([])
