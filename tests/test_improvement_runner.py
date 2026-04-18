from unittest.mock import MagicMock, patch

from ouroboros.improvement import ImprovementResult, ImprovementTask
from ouroboros.improvement_runner import run_scheduled_self_improvement
from ouroboros.moltbook import RunnerConfig
from ouroboros.model_defaults import DEFAULT_OPENAI_MODEL


def _cfg(**overrides):
    defaults = dict(
        enable_self_improvement=True,
        enable_auto_issue_creation=True,
        self_improvement_retry_minutes=60,
        improvement_interval_hours=6,
        improvement_model=DEFAULT_OPENAI_MODEL,
    )
    defaults.update(overrides)
    return RunnerConfig(**defaults)


@patch("ouroboros.improvement_runner.time.time", return_value=1_700_000_000)
@patch("ouroboros.improvement_runner.save_scheduler_state")
@patch("ouroboros.improvement_runner.load_scheduler_state", return_value={"consecutive_failures": 2, "next_due_ts": None})
@patch("ouroboros.improvement_runner._load_feed_context_state", return_value={})
@patch("ouroboros.improvement_runner.llm.make_client")
@patch("ouroboros.improvement_runner.llm.load_openai_key", return_value="key")
@patch("ouroboros.improvement_runner.run_improvement_cycle")
@patch("ouroboros.improvement_runner.git_ops.has_open_improvement_prs", return_value=False)
@patch("ouroboros.improvement_runner.git_ops.is_clean", return_value=True)
@patch("ouroboros.improvement_runner.check_pr_outcomes")
@patch("ouroboros.improvement_runner.get_repo_root")
@patch("ouroboros.improvement_runner.load_runner_config")
def test_run_scheduled_self_improvement_success(
    mock_cfg,
    mock_repo_root,
    _mock_check_prs,
    _mock_is_clean,
    _mock_has_open_prs,
    mock_run_cycle,
    _mock_load_key,
    _mock_make_client,
    _mock_feed_state,
    _mock_load_state,
    mock_save_state,
    _mock_time,
):
    mock_cfg.return_value = _cfg()
    mock_repo_root.return_value = "/tmp/repo"
    task = ImprovementTask("abc", "fix_bug", "Repair status output", ["src/ouroboros/cli.py"], "Bug")
    mock_run_cycle.return_value = ImprovementResult(task=task, status="success", pr_url="https://github.com/repo/pull/9")

    result = run_scheduled_self_improvement(force=True)

    assert result.status == "success"
    assert result.pr_url == "https://github.com/repo/pull/9"
    saved_state = mock_save_state.call_args.args[0]
    assert saved_state["consecutive_failures"] == 0
    assert saved_state["last_pr_url"] == "https://github.com/repo/pull/9"


@patch("ouroboros.improvement_runner.time.time", return_value=1_700_000_000)
@patch("ouroboros.improvement_runner.save_scheduler_state")
@patch("ouroboros.improvement_runner.load_scheduler_state", return_value={"consecutive_failures": 0, "next_due_ts": None})
@patch("ouroboros.improvement_runner._load_feed_context_state", return_value={})
@patch("ouroboros.improvement_runner.llm.make_client")
@patch("ouroboros.improvement_runner.llm.load_openai_key", return_value="key")
@patch("ouroboros.improvement_runner.git_ops.create_issue", return_value="https://github.com/repo/issues/13")
@patch("ouroboros.improvement_runner.git_ops.find_open_issue_by_marker", return_value=None)
@patch("ouroboros.improvement_runner.run_improvement_cycle")
@patch("ouroboros.improvement_runner.git_ops.has_open_improvement_prs", return_value=False)
@patch("ouroboros.improvement_runner.git_ops.is_clean", return_value=True)
@patch("ouroboros.improvement_runner.check_pr_outcomes")
@patch("ouroboros.improvement_runner.get_repo_root")
@patch("ouroboros.improvement_runner.load_runner_config")
def test_run_scheduled_self_improvement_creates_issue_on_failure(
    mock_cfg,
    mock_repo_root,
    _mock_check_prs,
    _mock_is_clean,
    _mock_has_open_prs,
    mock_run_cycle,
    _mock_find_issue,
    _mock_create_issue,
    _mock_load_key,
    _mock_make_client,
    _mock_feed_state,
    _mock_load_state,
    mock_save_state,
    _mock_time,
):
    mock_cfg.return_value = _cfg()
    mock_repo_root.return_value = "/tmp/repo"
    task = ImprovementTask("abc", "fix_bug", "Repair status output", ["src/ouroboros/cli.py"], "Bug")
    mock_run_cycle.return_value = ImprovementResult(
        task=task,
        status="failed",
        details="Failed to generate a concrete implementation plan",
    )

    result = run_scheduled_self_improvement(force=True)

    assert result.status == "failed"
    assert result.issue_url == "https://github.com/repo/issues/13"
    saved_state = mock_save_state.call_args.args[0]
    assert saved_state["consecutive_failures"] == 1
    assert saved_state["last_issue_url"] == "https://github.com/repo/issues/13"


@patch("ouroboros.improvement_runner.time.time", return_value=1_700_000_000)
@patch("ouroboros.improvement_runner.load_scheduler_state", return_value={"next_due_ts": 1_700_003_600})
@patch("ouroboros.improvement_runner.load_runner_config")
def test_run_scheduled_self_improvement_skips_until_due(mock_cfg, _mock_load_state, _mock_time):
    mock_cfg.return_value = _cfg()

    result = run_scheduled_self_improvement()

    assert result.status == "skipped_due"
