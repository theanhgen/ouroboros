import argparse
import json
from unittest.mock import MagicMock, patch
from ouroboros.cli import (
    cmd_plan,
    cmd_config_show,
    cmd_config_modify,
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
