import json
from unittest import mock

from ouroboros.self_modify import modify_runner_config


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
