import argparse
import json
from unittest.mock import MagicMock, patch
from ouroboros.cli import (
    cmd_plan,
    cmd_config_show,
    cmd_config_modify,
)

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
