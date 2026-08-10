import pytest
import argparse
import json
from unittest.mock import MagicMock, patch
from ouroboros.cli import (
    cmd_improve_status,
    cmd_plan,
    cmd_config_show,
    cmd_config_modify,
    cmd_improve_scout,
    cmd_moltbook_feed,
    cmd_moltbook_status,
)
from ouroboros.moltbook import MoltbookError

def test_cmd_plan(capsys):
    args = argparse.Namespace()
    ret = cmd_plan(args)
    assert ret == 0
    captured = capsys.readouterr()
    assert "Ouroboros plan" in captured.out

@patch("ouroboros.cli.get_current_config")
def test_cmd_config_show(mock_get, capsys):
    mock_get.return_value = {"test": "config"}
    args = argparse.Namespace()
    ret = cmd_config_show(args)
    assert ret == 0
    captured = capsys.readouterr()
    assert '"test": "config"' in captured.out

@patch("ouroboros.cli.can_self_modify")
@patch("ouroboros.cli.modify_runner_config")
def test_cmd_config_modify_success(mock_modify, mock_can, capsys):
    mock_can.return_value = True
    args = argparse.Namespace(updates=["interval=60", "enable=true", "name=test"])
    ret = cmd_config_modify(args)
    assert ret == 0
    mock_modify.assert_called_once_with({"interval": 60, "enable": True, "name": "test"})
    captured = capsys.readouterr()
    assert "Updated runner config" in captured.out

@patch("ouroboros.cli.can_self_modify")
def test_cmd_config_modify_disabled(mock_can, capsys):
    mock_can.return_value = False
    args = argparse.Namespace(updates=["k=v"])
    ret = cmd_config_modify(args)
    assert ret == 2
    captured = capsys.readouterr()
    assert "Self-modification is disabled" in captured.out


@patch("ouroboros.cli.load_credentials", side_effect=MoltbookError("Missing API key. Set MOLTBOOK_API_KEY or credentials.json"))
def test_cmd_moltbook_status_missing_credentials(mock_load, capsys):
    args = argparse.Namespace()
    ret = cmd_moltbook_status(args)
    assert ret == 2
    captured = capsys.readouterr()
    assert "requires MOLTBOOK_API_KEY" in captured.out


@patch("ouroboros.cli.load_credentials")
@patch("ouroboros.cli.get_feed")
def test_cmd_moltbook_feed_only_requires_api_key(mock_get_feed, mock_load, capsys):
    mock_load.return_value = MagicMock(api_key="mb-key")
    mock_get_feed.return_value = {"posts": []}
    args = argparse.Namespace(sort="new", limit=10)
    ret = cmd_moltbook_feed(args)
    assert ret == 0
    mock_load.assert_called_once_with(require_agent_name=False)
    captured = capsys.readouterr()
    assert "{'posts': []}" in captured.out


@patch("ouroboros.llm.make_client")
@patch("ouroboros.llm.load_openai_key", return_value="sk-test")
@patch("ouroboros.codebase.get_repo_root")
@patch("ouroboros.issue_scouting.run_issue_scouting_cycle")
def test_cmd_improve_scout_success(mock_scout, mock_repo_root, _mock_key, _mock_client, capsys):
    mock_repo_root.return_value = MagicMock()
    mock_scout.return_value = MagicMock(
        status="created",
        message="Opened issue",
        issue_url="https://github.com/repo/issues/42",
        task=MagicMock(task_type="fix_bug", description="Tighten retry handling"),
    )
    args = argparse.Namespace(model="gpt-5.4-nano-2026-03-17", dry_run=False)

    ret = cmd_improve_scout(args)

    assert ret == 0
    captured = capsys.readouterr()
    assert "Issue scout: [created] Opened issue" in captured.out
    assert "Issue: https://github.com/repo/issues/42" in captured.out


@pytest.mark.parametrize(
    ("lookup", "expected"),
    [(True, "yes"), (False, "no"), (None, "unknown")],
)
@patch("ouroboros.moltbook.load_state", return_value={})
@patch("ouroboros.improvement_runner.load_scheduler_state", return_value={})
@patch("ouroboros.evaluation.check_pr_outcomes", return_value=[])
@patch("ouroboros.codebase.get_repo_root", return_value="/repo")
@patch("ouroboros.git_ops.has_open_improvement_prs")
def test_status_reports_the_open_pr_state_in_three_forms(
    mock_has_open, _root, _outcomes, _sched, _loop, lookup, expected, capsys
):
    """Reporting an indeterminate lookup as "no" hides a degraded dependency."""
    mock_has_open.return_value = lookup

    cmd_improve_status(argparse.Namespace())

    assert f"Open improvement PRs: {expected}" in capsys.readouterr().out
