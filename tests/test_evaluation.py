"""Tests for evaluation module."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from ouroboros.evaluation import (
    EvaluationRecord,
    load_history,
    record_improvement,
    improvements_today,
    summarize_history,
)
from ouroboros.improvement import ImprovementTask, ImprovementResult, CodeChange
from ouroboros.test_runner import TestResult


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
    test_before = TestResult(passed=5, failed=0, errors=0, returncode=0)
    test_after = TestResult(passed=6, failed=0, errors=0, returncode=0)
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
