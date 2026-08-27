"""A cycle must record its outcome whichever way it ends.

The memory write used to sit at the bottom of `run_improvement_cycle`, after PR
creation. Four of the eleven exits return before reaching it: no plan, no code,
reviewer rejected, validation failed. So every failed cycle recorded nothing,
and production carried 544 facts in categories `code` and `success` with **zero
failure facts in five months** -- the one category the agent needs in order to
stop repeating itself.

These tests pin the structural property rather than the four instances: the
epilogue runs from a `finally`, so a twelfth exit added later cannot bypass it.
"""

import pytest

from ouroboros import improvement


class _Recorder:
    """Stands in for IndexManager and remembers what it was told."""

    instances = []

    def __init__(self, *_a, **_k):
        self.successes = []
        self.failures = []
        self.feedback = []
        self.indexed = []
        _Recorder.instances.append(self)

    def index_file(self, path, content):
        self.indexed.append(path)

    def index_success(self, task_id, description, details=""):
        self.successes.append((task_id, description, details))

    def index_failure(self, task_id, description, failure_msg=""):
        self.failures.append((task_id, description, failure_msg))

    def record_outcome_feedback(self, description, success):
        self.feedback.append((description, success))


@pytest.fixture
def recorder(monkeypatch):
    _Recorder.instances = []
    import ouroboros.memory as memory_mod
    monkeypatch.setattr(memory_mod, "IndexManager", _Recorder)
    return _Recorder


class _Task:
    task_id = "abc123"
    task_type = "fix_bug"
    description = "Fix the thing that is broken"


class _Result:
    def __init__(self, status, details="because reasons"):
        self.status = status
        self.details = details
        self.changes = []


# --------------------------------------------------------------- the epilogue

@pytest.mark.parametrize("status", ["failed", "reverted"])
def test_a_failing_cycle_records_a_failure_fact(recorder, status):
    ctx = {"task": _Task(), "result": _Result(status)}
    improvement._record_cycle_memory(ctx)

    assert len(recorder.instances) == 1
    rec = recorder.instances[0]
    assert rec.failures == [("abc123", "Fix the thing that is broken", "because reasons")]
    assert rec.feedback == [("Fix the thing that is broken", False)]
    assert rec.successes == []


def test_a_successful_cycle_records_a_success_fact(recorder):
    ctx = {"task": _Task(), "result": _Result("success", "tests 10 -> 12")}
    improvement._record_cycle_memory(ctx)

    rec = recorder.instances[0]
    assert rec.successes == [("abc123", "Fix the thing that is broken", "tests 10 -> 12")]
    assert rec.feedback == [("Fix the thing that is broken", True)]
    assert rec.failures == []


@pytest.mark.parametrize("ctx", [
    {},                                    # rate limit / open PR / stale streak
    {"task": _Task()},                     # identified, then dry run
    {"result": _Result("failed")},         # cannot happen, but must not raise
])
def test_a_legitimate_skip_records_nothing(recorder, ctx):
    """Rate limit, an open PR, a stale streak and a dry run are not failures and
    must not be written to memory as if they were."""
    improvement._record_cycle_memory(ctx)
    assert recorder.instances == []


def test_the_epilogue_never_raises(monkeypatch):
    """It runs from a `finally`. If it raised, a failure to *record* a cycle
    would turn a completed cycle into a crashed one."""
    import ouroboros.memory as memory_mod

    def boom(*_a, **_k):
        raise RuntimeError("memory database is on fire")

    monkeypatch.setattr(memory_mod, "IndexManager", boom)
    improvement._record_cycle_memory({"task": _Task(), "result": _Result("failed")})


# ------------------------------------------------------- the structural claim

def test_every_exit_runs_the_epilogue(monkeypatch, recorder):
    """The point of the split: whatever the body does -- return early at any of
    its eleven exits, or raise -- the epilogue still runs.

    Parametrised over behaviours rather than over the four known exits, because
    the property being defended is that a NEW exit cannot bypass it either.
    """
    task, result = _Task(), _Result("failed")

    def body_returns_early(ctx, *_a, **_k):
        ctx["task"] = task
        ctx["result"] = result
        return result

    def body_raises(ctx, *_a, **_k):
        ctx["task"] = task
        ctx["result"] = result
        raise RuntimeError("generation blew up")

    monkeypatch.setattr(improvement, "_run_improvement_cycle", body_returns_early)
    improvement.run_improvement_cycle(client=None, state={})
    assert recorder.instances[-1].failures, "an early return must still record"

    monkeypatch.setattr(improvement, "_run_improvement_cycle", body_raises)
    with pytest.raises(RuntimeError):
        improvement.run_improvement_cycle(client=None, state={})
    assert recorder.instances[-1].failures, "a raise must still record"


def test_the_wrapper_returns_what_the_body_returns(monkeypatch, recorder):
    sentinel = _Result("success")

    def body(ctx, *_a, **_k):
        ctx["task"] = _Task()
        ctx["result"] = sentinel
        return sentinel

    monkeypatch.setattr(improvement, "_run_improvement_cycle", body)
    assert improvement.run_improvement_cycle(client=None, state={}) is sentinel
