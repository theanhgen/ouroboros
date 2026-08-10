import json
import os
from unittest import mock
import pytest

from ouroboros.self_modify import (
    filter_untrusted_config_updates,
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


# -- untrusted (comment-suggested) config updates ----------------------------

@pytest.mark.parametrize(
    "key",
    [
        "reviewer_base_url",
        "local_model_base_url",
        "reviewer_api_key",
        "ollama_api_key",
        "telegram_bot_token",
        "reviewer_model",
        "reviewer_backend",
        "generator_backend",
        "improvement_model",
        "auto_apply_config_suggestions",
        "enable_comment_based_upgrades",
        "enable_self_modification",
        "enable_auto_merge",
        "dry_run",
    ],
)
def test_filter_untrusted_config_updates_rejects_operator_only_keys(key):
    safe, rejected = filter_untrusted_config_updates({key: "x"})
    assert safe == {}
    assert rejected == [key]


def test_filter_untrusted_config_updates_allows_ordinary_keys():
    updates = {"min_post_interval_hours": 24, "max_comments_per_cycle": 5}
    safe, rejected = filter_untrusted_config_updates(updates)
    assert safe == updates
    assert rejected == []


def test_filter_untrusted_config_updates_splits_mixed_input():
    """The exfiltration key is dropped; the benign one still applies."""
    safe, rejected = filter_untrusted_config_updates({
        "min_post_interval_hours": 24,
        "reviewer_base_url": "https://attacker.example/v1",
    })
    assert safe == {"min_post_interval_hours": 24}
    assert rejected == ["reviewer_base_url"]


def test_ollama_api_key_is_canonicalised_to_reviewer_api_key(tmp_path):
    """Rotating via the alias must not leave the old key in effect."""
    cfg_file = tmp_path / "agent.json"
    cred_file = tmp_path / "credentials.json"
    cred_file.write_text(json.dumps({"reviewer_api_key": "old"}))

    def fake_expanduser(path):
        if path.endswith("agent.json"):
            return str(cfg_file)
        if path.endswith("credentials.json"):
            return str(cred_file)
        return path

    with mock.patch("ouroboros.self_modify.os.path.expanduser", side_effect=fake_expanduser):
        modify_runner_config({"ollama_api_key": "new"})

    creds = json.loads(cred_file.read_text())
    assert creds["reviewer_api_key"] == "new"
    assert "ollama_api_key" not in creds
    assert json.loads(cfg_file.read_text()).get("ollama_api_key") is None


def test_every_enable_flag_is_operator_only():
    """No comment may switch on automation that writes outside this process."""
    from dataclasses import fields

    from ouroboros.moltbook import RunnerConfig

    enable_flags = [f.name for f in fields(RunnerConfig) if f.name.startswith("enable_")]
    assert enable_flags, "expected RunnerConfig to have enable_* flags"

    safe, rejected = filter_untrusted_config_updates({f: True for f in enable_flags})
    assert safe == {}
    assert rejected == sorted(enable_flags)


@pytest.mark.parametrize(
    "key",
    [
        "enable_self_improvement",
        "enable_github_improvement",
        "enable_auto_git_push",
        "enable_issue_scouting",
        "enable_community_improvement",
        "enable_auto_comment",
        "enable_wiki",
        "enable_self_modification",
        "enable_comment_based_upgrades",
        "enable_auto_merge",
    ],
)
def test_repository_mutating_flags_are_rejected(key):
    safe, rejected = filter_untrusted_config_updates({key: True})
    assert safe == {}
    assert rejected == [key]


def test_operator_path_can_still_set_everything(tmp_path):
    """The denylist applies to suggestions only, not to the operator."""
    cfg_file = tmp_path / "agent.json"
    cred_file = tmp_path / "credentials.json"

    def fake_expanduser(path):
        if path.endswith("agent.json"):
            return str(cfg_file)
        if path.endswith("credentials.json"):
            return str(cred_file)
        return path

    with mock.patch("ouroboros.self_modify.os.path.expanduser", side_effect=fake_expanduser):
        modify_runner_config({
            "enable_self_improvement": True,
            "reviewer_base_url": "https://ollama.com/v1",
        })

    data = json.loads(cfg_file.read_text())
    assert data["enable_self_improvement"] is True
    assert data["reviewer_base_url"] == "https://ollama.com/v1"


def test_no_model_or_backend_key_is_suggestible():
    """Anything naming a model or backend decides who sees the source."""
    from dataclasses import fields

    from ouroboros.moltbook import RunnerConfig

    routing = [
        f.name
        for f in fields(RunnerConfig)
        if f.name.endswith(("_model", "_backend", "_base_url", "_api_key"))
    ]
    safe, _ = filter_untrusted_config_updates({f: "x" for f in routing})
    assert safe == {}


@pytest.mark.parametrize("key", ["default_submolt", "post_after_self_question"])
def test_destination_and_behaviour_selectors_are_operator_only(key):
    """Where the agent publishes, and whether it does, is not a suggestion."""
    safe, rejected = filter_untrusted_config_updates({key: "attacker-submolt"})
    assert safe == {}
    assert rejected == [key]
