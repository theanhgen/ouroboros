from pathlib import Path
from unittest.mock import patch

from ouroboros.improvement import ImprovementTask
from ouroboros.issue_scouting import run_issue_scouting_cycle
from ouroboros.test_runner import RunnerOutcome


def _task() -> ImprovementTask:
    return ImprovementTask(
        task_id="task-1",
        task_type="fix_bug",
        description="Tighten retry handling around stalled queue workers",
        target_files=["src/ouroboros/queue.py", "tests/test_queue.py"],
        evidence="Worker retries can double-submit tasks after timeout.",
    )


@patch("ouroboros.issue_scouting.git_ops.create_issue")
@patch("ouroboros.issue_scouting.git_ops.find_open_issue_by_marker", return_value=None)
@patch("ouroboros.issue_scouting.identify_improvements")
@patch("ouroboros.issue_scouting.run_tests")
@patch("ouroboros.issue_scouting.get_codebase_summary", return_value="summary")
@patch("ouroboros.issue_scouting.load_history", return_value=[])
def test_run_issue_scouting_cycle_creates_issue(
    _mock_history,
    _mock_summary,
    mock_run_tests,
    mock_identify,
    _mock_find_open,
    mock_create_issue,
):
    mock_run_tests.return_value = RunnerOutcome(passed=12, failed=1, errors=0, returncode=1)
    mock_identify.return_value = _task()
    mock_create_issue.return_value = "https://github.com/repo/issues/42"

    result = run_issue_scouting_cycle(object(), Path("/tmp/repo"))

    assert result.status == "created"
    assert result.issue_url == "https://github.com/repo/issues/42"
    assert result.task.description.startswith("Tighten retry handling")
    issue_body = mock_create_issue.call_args.args[2]
    assert "Autonomous Improvement Opportunity" in issue_body
    assert "<!-- ouroboros:auto-issue:" in issue_body


@patch("ouroboros.issue_scouting.git_ops.create_issue")
@patch("ouroboros.issue_scouting.git_ops.find_open_issue_by_marker", return_value="https://github.com/repo/issues/9")
@patch("ouroboros.issue_scouting.identify_improvements")
@patch("ouroboros.issue_scouting.run_tests")
@patch("ouroboros.issue_scouting.get_codebase_summary", return_value="summary")
@patch("ouroboros.issue_scouting.load_history", return_value=[])
def test_run_issue_scouting_cycle_deduplicates_existing_issue(
    _mock_history,
    _mock_summary,
    mock_run_tests,
    mock_identify,
    _mock_find_open,
    mock_create_issue,
):
    mock_run_tests.return_value = RunnerOutcome(passed=12, failed=0, errors=0, returncode=0)
    mock_identify.return_value = _task()

    result = run_issue_scouting_cycle(object(), Path("/tmp/repo"))

    assert result.status == "duplicate"
    assert result.issue_url == "https://github.com/repo/issues/9"
    mock_create_issue.assert_not_called()


@patch("ouroboros.issue_scouting.git_ops.create_issue")
@patch("ouroboros.issue_scouting.git_ops.find_open_issue_by_marker", return_value=None)
@patch("ouroboros.issue_scouting.identify_improvements")
@patch("ouroboros.issue_scouting.run_tests")
@patch("ouroboros.issue_scouting.get_codebase_summary", return_value="summary")
@patch("ouroboros.issue_scouting.load_history", return_value=[])
def test_run_issue_scouting_cycle_dry_run(
    _mock_history,
    _mock_summary,
    mock_run_tests,
    mock_identify,
    _mock_find_open,
    mock_create_issue,
):
    mock_run_tests.return_value = RunnerOutcome(passed=12, failed=0, errors=0, returncode=0)
    mock_identify.return_value = _task()

    result = run_issue_scouting_cycle(object(), Path("/tmp/repo"), dry_run=True)

    assert result.status == "dry_run"
    assert "Would open issue" in result.message
    mock_create_issue.assert_not_called()
