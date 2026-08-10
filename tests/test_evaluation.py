"""Tests for evaluation module."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from ouroboros.evaluation import (
    EvaluationRecord,
    check_pr_outcomes,
    load_history,
    record_improvement,
    improvements_today,
    summarize_history,
)
from ouroboros.improvement import ImprovementTask, ImprovementResult, CodeChange
from ouroboros.test_runner import RunnerOutcome


def test_evaluation_record_roundtrip():
    record = EvaluationRecord(
        task_id="abc",
        task_type="fix_test",
        description="Fix test_foo",
        test_delta={"before": {"passed": 5, "failed": 1}, "after": {"passed": 6, "failed": 0}},
        pr_url="https://github.com/test/pr/1",
        outcome="merged",
        timestamp=1000.0,
    )
    d = record.to_dict()
    restored = EvaluationRecord.from_dict(d)
    assert restored.task_id == "abc"
    assert restored.task_type == "fix_test"
    assert restored.outcome == "merged"
    assert restored.pr_url == "https://github.com/test/pr/1"


def test_load_history_empty(tmp_path):
    history = load_history(tmp_path)
    assert history == []


def test_load_history_with_data(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    history_file = config_dir / "improvement_history.json"
    data = [
        {
            "task_id": "abc",
            "task_type": "fix_test",
            "description": "test fix",
            "test_delta": {},
            "pr_url": "",
            "outcome": "merged",
            "feedback": "",
            "timestamp": 1000.0,
        }
    ]
    history_file.write_text(json.dumps(data))

    history = load_history(tmp_path)
    assert len(history) == 1
    assert history[0].task_id == "abc"


def test_record_improvement(tmp_path):
    task = ImprovementTask("xyz", "add_test", "add test", ["tests/t.py"], "missing")
    test_before = RunnerOutcome(passed=5, failed=0, errors=0, returncode=0)
    test_after = RunnerOutcome(passed=6, failed=0, errors=0, returncode=0)
    result = ImprovementResult(
        task=task,
        changes=[],
        test_before=test_before,
        test_after=test_after,
        pr_url="https://github.com/test/pr/2",
        status="success",
    )

    record_improvement(result, tmp_path)

    history = load_history(tmp_path)
    assert len(history) == 1
    assert history[0].task_type == "add_test"
    assert history[0].pr_url == "https://github.com/test/pr/2"


def test_improvements_today(tmp_path):
    import time

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    history_file = config_dir / "improvement_history.json"
    now = time.time()
    data = [
        {"task_id": "a", "task_type": "fix", "description": "d", "test_delta": {},
         "pr_url": "", "outcome": "success", "feedback": "", "timestamp": now},
        {"task_id": "b", "task_type": "fix", "description": "d", "test_delta": {},
         "pr_url": "", "outcome": "success", "feedback": "", "timestamp": now - 100000},
    ]
    history_file.write_text(json.dumps(data))

    count = improvements_today(tmp_path)
    assert count == 1  # only the recent one


def test_summarize_history_empty():
    result = summarize_history([])
    assert "No previous" in result


def test_summarize_history():
    records = [
        EvaluationRecord(
            task_id="a", task_type="fix_test", description="fix test_foo",
            test_delta={"before": {"passed": 5, "failed": 1}, "after": {"passed": 6, "failed": 0}},
            outcome="merged", timestamp=1000.0,
        ),
        EvaluationRecord(
            task_id="b", task_type="add_test", description="add coverage",
            outcome="closed", feedback="Not needed", timestamp=2000.0,
        ),
    ]
    summary = summarize_history(records)
    assert "fix_test" in summary
    assert "merged" in summary
    assert "closed" in summary
    assert "Not needed" in summary


@patch("ouroboros.evaluation.git_ops.get_pr_feedback", return_value="Looks good")
@patch("subprocess.run")
def test_check_pr_outcomes_updates_success_records(mock_run, _mock_feedback, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    history_file = config_dir / "improvement_history.json"
    history_file.write_text(json.dumps([
        {
            "task_id": "abc",
            "task_type": "fix_test",
            "description": "test fix",
            "test_delta": {},
            "pr_url": "https://github.com/test/pr/1",
            "outcome": "success",
            "feedback": "",
            "timestamp": 1000.0,
        }
    ]))

    mock_run.return_value = MagicMock(stdout="MERGED\n")

    history = check_pr_outcomes(tmp_path)

    assert history[0].outcome == "merged"
    assert history[0].feedback == "Looks good"

    # Persistence is now SQLite, so read it back the way callers do rather
    # than inspecting the legacy JSON file.
    assert load_history(tmp_path)[0].outcome == "merged"


# -- centralised JSON IO -----------------------------------------------------

def test_history_write_is_atomic(tmp_path, monkeypatch):
    """A crash mid-write must not leave a truncated history file."""
    import ouroboros.evaluation as ev

    path = tmp_path / "config" / "improvement_history.json"
    path.parent.mkdir(parents=True)
    path.write_text("[]")
    monkeypatch.setattr(ev, "_history_path", lambda root=None: path)

    real_replace = os.replace
    calls = []

    def failing_replace(src, dst):
        calls.append((src, dst))
        raise OSError("simulated crash before rename")

    monkeypatch.setattr("ouroboros.storage.os.replace", failing_replace)

    with pytest.raises(OSError):
        ev.save_json_file(path, [{"a": 1}])

    # The original file is untouched -- the partial write went to a temp file.
    assert path.read_text() == "[]"
    assert calls, "expected an atomic rename to have been attempted"
    monkeypatch.setattr("ouroboros.storage.os.replace", real_replace)


def test_load_history_tolerates_a_corrupt_file(tmp_path, monkeypatch):
    import ouroboros.evaluation as ev

    path = tmp_path / "improvement_history.json"
    path.write_text("{ not json")
    monkeypatch.setattr(ev, "_history_path", lambda root=None: path)

    assert ev.load_history() == []


def test_load_history_missing_file_is_empty(tmp_path, monkeypatch):
    import ouroboros.evaluation as ev

    monkeypatch.setattr(ev, "_history_path", lambda root=None: tmp_path / "nope.json")
    assert ev.load_history() == []


def test_unreadable_history_is_not_silently_replaced(tmp_path, monkeypatch):
    """An I/O failure must not let record_improvement overwrite real history."""
    import ouroboros.evaluation as ev

    path = tmp_path / "improvement_history.json"
    path.write_text(json.dumps([{"task_type": "fix_bug"}]))
    monkeypatch.setattr(ev, "_history_path", lambda root=None: path)

    original = path.read_text()

    def unreadable(*a, **kw):
        raise PermissionError("cannot read")

    monkeypatch.setattr(Path, "open", unreadable)

    with pytest.raises(PermissionError):
        ev.load_history()

    monkeypatch.undo()
    assert path.read_text() == original


@pytest.mark.parametrize("payload", ["{}", "null", '"a string"', "42"])
def test_load_history_non_list_payload(tmp_path, monkeypatch, payload):
    import ouroboros.evaluation as ev

    path = tmp_path / "improvement_history.json"
    path.write_text(payload)
    monkeypatch.setattr(ev, "_history_path", lambda root=None: path)
    assert ev.load_history() == []


def test_load_history_binary_garbage(tmp_path, monkeypatch):
    import ouroboros.evaluation as ev

    path = tmp_path / "improvement_history.json"
    path.write_bytes(b"\x00\x81\xfe" * 100)
    monkeypatch.setattr(ev, "_history_path", lambda root=None: path)
    assert ev.load_history() == []


def test_unfetchable_feedback_leaves_the_record_open(tmp_path, monkeypatch):
    """A terminal record is never polled again, so it must not be finalised
    before its review text has actually been retrieved."""
    import ouroboros.evaluation as ev

    path = tmp_path / "improvement_history.json"
    path.write_text(json.dumps([{
        "task_id": "abc", "task_type": "fix_bug", "description": "d",
        "test_delta": {}, "pr_url": "https://github.com/x/pull/1",
        "outcome": "success", "feedback": "", "timestamp": 1000.0,
    }]))
    monkeypatch.setattr(ev, "_history_path", lambda root=None: path)

    with patch("subprocess.run", return_value=MagicMock(stdout="MERGED\n")):
        with patch.object(ev.git_ops, "get_pr_feedback", return_value=None):
            history = ev.check_pr_outcomes(tmp_path)

    assert history[0].outcome == "success", "must stay pollable"
    assert json.loads(path.read_text())[0]["outcome"] == "success"


def test_fetched_feedback_finalises_the_record(tmp_path, monkeypatch):
    import ouroboros.evaluation as ev

    path = tmp_path / "improvement_history.json"
    path.write_text(json.dumps([{
        "task_id": "abc", "task_type": "fix_bug", "description": "d",
        "test_delta": {}, "pr_url": "https://github.com/x/pull/1",
        "outcome": "success", "feedback": "", "timestamp": 1000.0,
    }]))
    monkeypatch.setattr(ev, "_history_path", lambda root=None: path)

    with patch("subprocess.run", return_value=MagicMock(stdout="MERGED\n")):
        with patch.object(ev.git_ops, "get_pr_feedback", return_value="Nice work"):
            history = ev.check_pr_outcomes(tmp_path)

    assert history[0].outcome == "merged"
    assert history[0].feedback == "Nice work"


def test_a_merged_pr_with_genuinely_no_feedback_still_finalises(tmp_path, monkeypatch):
    """"" is a real answer; only None means the fetch failed."""
    import ouroboros.evaluation as ev

    path = tmp_path / "improvement_history.json"
    path.write_text(json.dumps([{
        "task_id": "abc", "task_type": "fix_bug", "description": "d",
        "test_delta": {}, "pr_url": "https://github.com/x/pull/1",
        "outcome": "success", "feedback": "", "timestamp": 1000.0,
    }]))
    monkeypatch.setattr(ev, "_history_path", lambda root=None: path)

    with patch("subprocess.run", return_value=MagicMock(stdout="MERGED\n")):
        with patch.object(ev.git_ops, "get_pr_feedback", return_value=""):
            history = ev.check_pr_outcomes(tmp_path)

    assert history[0].outcome == "merged"
