import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from ouroboros.improvement_runner import (
    _next_due_ts,
    _retry_delay_seconds,
    _set_idle_state,
    _set_deferred_state,
    _set_failure_state,
    _task_issue_marker,
    _build_followup_issue_body,
    _maybe_create_followup_issue,
    run_scheduled_self_improvement,
    ScheduledImprovementRun,
)
from ouroboros.improvement import ImprovementTask, ImprovementResult

class TestImprovementRunner:
    def setup_method(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.config_dir = self.tmp_dir / ".config" / "moltbook"
        self.config_dir.mkdir(parents=True)

    def teardown_method(self):
        shutil.rmtree(self.tmp_dir)

    def test_retry_delay_seconds(self):
        cfg = MagicMock(self_improvement_retry_minutes=60, improvement_interval_hours=24)
        # failure_count = 1 -> base * 2^0 = 60 * 60 = 3600
        assert _retry_delay_seconds(cfg, 1) == 3600
        # failure_count = 2 -> base * 2^1 = 3600 * 2 = 7200
        assert _retry_delay_seconds(cfg, 2) == 7200
        # failure_count = 10 -> capped at normal delay (24 * 3600 = 86400)
        assert _retry_delay_seconds(cfg, 10) == 86400

    def test_next_due_ts(self):
        cfg = MagicMock(improvement_interval_hours=2)
        # Case 1: next_due_ts explicitly set in state
        state = {"next_due_ts": 5000}
        assert _next_due_ts(state, cfg) == 5000
        
        # Case 2: calculated from last_attempt
        state = {"next_due_ts": None, "last_attempt": 1000}
        assert _next_due_ts(state, cfg) == 1000 + (2 * 3600)
        
        # Case 3: no data
        state = {"next_due_ts": None, "last_attempt": None}
        assert _next_due_ts(state, cfg) is None

    def test_state_transitions(self):
        cfg = MagicMock(improvement_interval_hours=1, self_improvement_retry_minutes=60)
        state = {}
        now = 10000
        
        _set_idle_state(state, now, cfg, "success", pr_url="http://pr")
        assert state["consecutive_failures"] == 0
        assert state["last_attempt"] == now
        assert state["last_pr_url"] == "http://pr"
        assert state["next_due_ts"] == now + 3600

        _set_failure_state(state, now, cfg, "failed", "some error")
        assert state["consecutive_failures"] == 1
        assert state["last_error"] == "some error"
        # failure_count=1 retry is 1hr (default cfg.self_improvement_retry_minutes=60)
        print(f"DEBUG: next_due_ts={state['next_due_ts']}, now={now}, diff={state['next_due_ts'] - now}")
        assert state["next_due_ts"] == now + 3600

    def test_task_issue_marker_stable(self):
        task = ImprovementTask(task_id="t1", task_type="feat", description="desc", target_files=["a.py"], evidence="ev")
        marker1 = _task_issue_marker(task)
        marker2 = _task_issue_marker(task)
        assert marker1 == marker2
        assert "ouroboros:auto-issue" in marker1

    @patch("ouroboros.git_ops.find_open_issue_by_marker")
    @patch("ouroboros.git_ops.create_issue")
    def test_maybe_create_followup_issue(self, mock_create, mock_find):
        cfg = MagicMock(enable_auto_issue_creation=True)
        task = ImprovementTask(task_id="t2", task_type="fix", description="bug", target_files=["f.py"], evidence="ev")
        result = ImprovementResult(status="failed", details="tests failed", task=task)
        
        # Case 1: already exists
        mock_find.return_value = "http://issue/1"
        url = _maybe_create_followup_issue(MagicMock(), cfg, result)
        assert url == "http://issue/1"
        mock_create.assert_not_called()

        # Case 2: create new
        mock_find.return_value = None
        mock_create.return_value = "http://issue/2"
        url = _maybe_create_followup_issue(MagicMock(), cfg, result)
        assert url == "http://issue/2"
        mock_create.assert_called_once()

    @patch("ouroboros.improvement_runner._scheduler_state_path")
    @patch("ouroboros.improvement_runner.load_runner_config")
    @patch("ouroboros.improvement_runner.get_repo_root")
    @patch("ouroboros.git_ops.is_clean")
    @patch("ouroboros.improvement_runner.run_improvement_cycle")
    @patch("ouroboros.llm.load_openai_key")
    @patch("ouroboros.llm.make_client")
    def test_run_scheduled_improvement_dirty_repo(self, mock_client, mock_key, mock_run_cycle, mock_is_clean, mock_root, mock_cfg, mock_state_path):
        mock_state_path.return_value = str(self.config_dir / "state.json")
        mock_cfg.return_value = MagicMock(enable_self_improvement=True)
        mock_is_clean.return_value = False
        
        run = run_scheduled_self_improvement()
        assert run.status == "skipped_dirty_repo"
        
        # Verify state was updated
        with open(mock_state_path.return_value, "r") as f:
            state = json.load(f)
            assert state["last_status"] == "skipped_dirty_repo"
            assert state["next_due_ts"] is not None

    @patch("ouroboros.improvement_runner._scheduler_state_path")
    @patch("ouroboros.improvement_runner.load_runner_config")
    @patch("ouroboros.improvement_runner.get_repo_root")
    @patch("ouroboros.git_ops.is_clean")
    @patch("ouroboros.git_ops.has_open_improvement_prs")
    @patch("ouroboros.evaluation.check_pr_outcomes")
    @patch("ouroboros.improvement_runner.run_improvement_cycle")
    @patch("ouroboros.llm.load_openai_key")
    @patch("ouroboros.llm.make_client")
    def test_run_scheduled_improvement_success(self, mock_client, mock_key, mock_run_cycle, mock_check_prs, mock_has_prs, mock_is_clean, mock_root, mock_cfg, mock_state_path):
        mock_state_path.return_value = str(self.config_dir / "state.json")
        cfg = MagicMock(enable_self_improvement=True, improvement_interval_hours=1)
        mock_cfg.return_value = cfg
        mock_is_clean.return_value = True
        mock_has_prs.return_value = False
        
        task = ImprovementTask(task_id="t3", task_type="refactor", description="clean code", target_files=[], evidence="")
        mock_run_cycle.return_value = ImprovementResult(status="success", task=task, pr_url="http://pr/123")
        
        run = run_scheduled_self_improvement()
        assert run.status == "success"
        assert run.pr_url == "http://pr/123"
        
        with open(mock_state_path.return_value, "r") as f:
            state = json.load(f)
            assert state["last_status"] == "success"
            assert state["consecutive_failures"] == 0
