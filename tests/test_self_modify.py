import json
import os
from unittest import mock
import pytest

from ouroboros.self_modify import (
    modify_runner_config,
    modify_config,
    can_self_modify,
    SelfModificationError,
)

def test_modify_runner_config_redirects_telegram_secrets(tmp_path):
    cfg_file = tmp_path / "agent.json"
    cred_file = tmp_path / "credentials.json"
    cfg_file.write_text(json.dumps({"interval_seconds": 900, "telegram_bot_token": "old"}))

    def fake_expanduser(path):
        if path == "~/.config/moltbook/agent.json":
            return str(cfg_file)
        if path == "~/.config/moltbook/credentials.json":
            return str(cred_file)
        return path

    with mock.patch("ouroboros.self_modify.os.path.expanduser", side_effect=fake_expanduser):
        modify_runner_config(
            {
                "interval_seconds": 60,
                "telegram_bot_token": "new-token",
                "telegram_chat_id": "12345",
            }
        )

    cfg_data = json.loads(cfg_file.read_text())
    cred_data = json.loads(cred_file.read_text())

    assert cfg_data["interval_seconds"] == 60
    assert "telegram_bot_token" not in cfg_data
    assert "telegram_chat_id" not in cfg_data
    assert cred_data["telegram_bot_token"] == "new-token"
    assert cred_data["telegram_chat_id"] == "12345"

@mock.patch("ouroboros.self_modify.can_self_modify")
def test_modify_config_disabled(mock_can):
    mock_can.return_value = False
    with pytest.raises(SelfModificationError, match="Self-modification is disabled"):
        modify_config({"key": "val"})

@mock.patch("ouroboros.self_modify.can_self_modify")
def test_modify_config_safety_error(mock_can):
    mock_can.return_value = True
    with pytest.raises(SelfModificationError, match="Safety config modification requires code changes"):
        modify_config({"key": "val"}, config_type="safety")

@mock.patch("ouroboros.self_modify.can_self_modify")
@mock.patch("ouroboros.self_modify.modify_runner_config")
def test_modify_config_runner_delegation(mock_modify_runner, mock_can):
    mock_can.return_value = True
    updates = {"interval": 10}
    modify_config(updates, config_type="runner")
    mock_modify_runner.assert_called_once_with(updates)

@mock.patch("ouroboros.config.SafetyConfig")
def test_can_self_modify(mock_safety):
    mock_instance = mock_safety.return_value
    mock_instance.allow_self_modification = True
    assert can_self_modify() is True
    
    mock_instance.allow_self_modification = False
    assert can_self_modify() is False
