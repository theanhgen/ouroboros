import pytest
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
@patch("ouroboros.improvement_runner.git_ops.list_open_issues", return_value=[])
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


@pytest.mark.parametrize(
    ("open_pr_lookup", "expected_status"),
    [
        (True, "skipped_open_pr"),
        # None means gh could not answer. Treating that as "no open PR" would
        # let the agent open a second PR for work already in flight.
        (None, "skipped_open_pr"),
    ],
)
@patch("ouroboros.improvement_runner.time.time", return_value=1_700_000_000)
@patch("ouroboros.improvement_runner.save_scheduler_state")
@patch("ouroboros.improvement_runner.load_scheduler_state",
       # A fresh dict per call: the runner mutates the state it is handed, so a
       # shared return_value leaks next_due_ts into the next parametrised case.
       side_effect=lambda: {"consecutive_failures": 0, "next_due_ts": None})
@patch("ouroboros.improvement_runner._load_feed_context_state", return_value={})
@patch("ouroboros.improvement_runner.run_improvement_cycle")
@patch("ouroboros.improvement_runner.git_ops.has_open_improvement_prs")
@patch("ouroboros.improvement_runner.git_ops.is_clean", return_value=True)
@patch("ouroboros.improvement_runner.check_pr_outcomes")
@patch("ouroboros.improvement_runner.get_repo_root")
@patch("ouroboros.improvement_runner.load_runner_config")
def test_scheduled_run_defers_unless_open_pr_state_is_definitely_false(
    mock_cfg,
    mock_repo_root,
    _mock_check_prs,
    _mock_is_clean,
    mock_has_open_prs,
    mock_run_cycle,
    _mock_feed_state,
    _mock_load_state,
    _mock_save_state,
    _mock_time,
    open_pr_lookup,
    expected_status,
):
    mock_cfg.return_value = _cfg()
    mock_repo_root.return_value = "/tmp/repo"
    mock_has_open_prs.return_value = open_pr_lookup

    result = run_scheduled_self_improvement()

    assert result.status == expected_status
    mock_run_cycle.assert_not_called()


@patch("ouroboros.improvement_runner.time.time", return_value=1_700_000_000)
@patch("ouroboros.improvement_runner.save_scheduler_state")
@patch("ouroboros.improvement_runner.load_scheduler_state", return_value={"consecutive_failures": 0, "next_due_ts": None})
@patch("ouroboros.improvement_runner._load_feed_context_state", return_value={})
@patch("ouroboros.improvement_runner.llm.make_client")
@patch("ouroboros.improvement_runner.llm.load_openai_key", return_value="key")
@patch("ouroboros.improvement_runner.run_improvement_cycle")
@patch("ouroboros.improvement_runner.git_ops.has_open_improvement_prs", return_value=False)
@patch("ouroboros.improvement_runner.git_ops.is_clean", return_value=True)
@patch("ouroboros.improvement_runner.check_pr_outcomes")
@patch("ouroboros.improvement_runner.get_repo_root")
@patch("ouroboros.improvement_runner.load_runner_config")
def test_the_configured_daily_cap_reaches_the_cycle(
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
    _mock_save_state,
    _mock_time,
):
    """The rate limit is read off SafetyConfig, not off the runner config.

    This is the scheduled path, which owns the improvement clock -- so a cap
    the operator set has to survive the handover or it is silently the
    SafetyConfig default that binds.
    """
    mock_cfg.return_value = _cfg(max_improvements_per_day=5)
    mock_repo_root.return_value = "/tmp/repo"
    task = ImprovementTask("abc", "fix_bug", "Repair status output", ["src/ouroboros/cli.py"], "Bug")
    mock_run_cycle.return_value = ImprovementResult(task=task, status="success", pr_url=None)

    run_scheduled_self_improvement(force=True)

    safety = mock_run_cycle.call_args.args[2]
    assert safety.max_improvements_per_day == 5


@patch("ouroboros.improvement_runner.time.time", return_value=1_700_000_000)
@patch("ouroboros.improvement_runner.save_scheduler_state")
@patch("ouroboros.improvement_runner.load_scheduler_state", return_value={"consecutive_failures": 0, "next_due_ts": None})
@patch("ouroboros.improvement_runner._load_feed_context_state", return_value={})
@patch("ouroboros.improvement_runner.llm.make_client")
@patch("ouroboros.improvement_runner.llm.load_openai_key", return_value="key")
@patch("ouroboros.improvement_runner.run_improvement_cycle")
@patch("ouroboros.improvement_runner.git_ops.has_open_improvement_prs", return_value=False)
@patch("ouroboros.improvement_runner.git_ops.is_clean", return_value=True)
@patch("ouroboros.improvement_runner.git_ops.pull_latest")
@patch("ouroboros.improvement_runner.check_pr_outcomes")
@patch("ouroboros.improvement_runner.get_repo_root")
@patch("ouroboros.improvement_runner.load_runner_config")
def test_run_scheduled_self_improvement_pulls_latest_first(
    mock_cfg,
    mock_repo_root,
    mock_check_prs,
    mock_pull_latest,
    _mock_is_clean,
    _mock_has_open_prs,
    mock_run_cycle,
    _mock_load_key,
    _mock_make_client,
    _mock_feed_state,
    _mock_load_state,
    _mock_save_state,
    _mock_time,
):
    mock_cfg.return_value = _cfg()
    mock_repo_root.return_value = "/tmp/repo"
    mock_run_cycle.return_value = ImprovementResult(task=None, status="idle")

    run_scheduled_self_improvement(force=True)

    mock_pull_latest.assert_called_once_with("/tmp/repo")
    mock_check_prs.assert_called_once_with("/tmp/repo", enable_auto_merge=False)



def test_interval_minutes_overrides_hours_with_a_floor():
    from ouroboros.improvement_runner import _normal_delay_seconds

    assert _normal_delay_seconds(_cfg(improvement_interval_hours=4)) == 4 * 3600
    assert _normal_delay_seconds(_cfg(improvement_interval_minutes=15)) == 15 * 60
    assert _normal_delay_seconds(_cfg(improvement_interval_minutes=1)) == 10 * 60


# -- follow-up issue deduplication --------------------------------------------

def _followup_body(files, marker="<!-- ouroboros:auto-issue:0123456789ab -->"):
    from ouroboros.improvement_runner import _build_followup_issue_body
    task = ImprovementTask("t", "fix_bug", "Earlier wording of the task", files, "why")
    return _build_followup_issue_body(task, ImprovementResult(task=task, status="failed"), marker)


def _failed(description, files):
    task = ImprovementTask("t", "fix_bug", description, files, "why")
    return ImprovementResult(task=task, status="failed", details="Generation failed")


@patch("ouroboros.improvement_runner.git_ops.create_issue")
@patch("ouroboros.improvement_runner.git_ops.list_open_issues")
def test_reworded_task_on_same_file_reuses_open_followup(mock_list, mock_create):
    """The 2026-09-24..28 flood: same stuck task, new wording each attempt,
    one new issue per attempt."""
    from ouroboros.improvement_runner import _maybe_create_followup_issue
    mock_list.return_value = [
        {"url": "https://github.com/repo/issues/300",
         "body": _followup_body(["/home/pi/ouroboros/src/ouroboros/memory.py"])},
    ]
    url = _maybe_create_followup_issue(
        "/tmp/repo", _cfg(), _failed("Fix index_code to use CodeASTVisitor", ["src/ouroboros/memory.py"]))
    assert url == "https://github.com/repo/issues/300"
    mock_create.assert_not_called()


@patch("ouroboros.improvement_runner.git_ops.create_issue", return_value="https://github.com/repo/issues/301")
@patch("ouroboros.improvement_runner.git_ops.list_open_issues")
def test_failure_on_other_files_still_gets_its_own_issue(mock_list, mock_create):
    from ouroboros.improvement_runner import _maybe_create_followup_issue
    mock_list.return_value = [
        {"url": "https://github.com/repo/issues/300", "body": _followup_body(["src/ouroboros/memory.py"])},
        # A human issue naming the file is not a follow-up and must not absorb it.
        {"url": "https://github.com/repo/issues/87",
         "body": "### Candidate files\n- `src/ouroboros/backlog.py`\n"},
    ]
    url = _maybe_create_followup_issue(
        "/tmp/repo", _cfg(), _failed("Guard backlog writes", ["src/ouroboros/backlog.py"]))
    assert url == "https://github.com/repo/issues/301"
    mock_create.assert_called_once()


@patch("ouroboros.improvement_runner.git_ops.create_issue", return_value="https://github.com/repo/issues/302")
@patch("ouroboros.improvement_runner.git_ops.list_open_issues", return_value=None)
def test_unlistable_issues_still_file_the_followup(_mock_list, mock_create):
    """gh down is not evidence of an existing issue; dropping the failure would
    lose it."""
    from ouroboros.improvement_runner import _maybe_create_followup_issue
    url = _maybe_create_followup_issue(
        "/tmp/repo", _cfg(), _failed("Anything", ["src/ouroboros/memory.py"]))
    assert url == "https://github.com/repo/issues/302"
