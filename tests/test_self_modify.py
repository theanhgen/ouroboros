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

def _reject_reason(updates, current=None):
    safe, rejected = filter_untrusted_config_updates(updates, current or {})
    return safe, " ".join(rejected)


@pytest.mark.parametrize(
    "key",
    [
        # Endpoint redirection and credentials.
        "reviewer_base_url",
        "local_model_base_url",
        "reviewer_api_key",
        "telegram_bot_token",
        # Model and backend routing -- chooses who sees the code.
        "reviewer_model",
        "generator_backend",
        "improvement_model",
        # Capability switches.
        "enable_self_improvement",
        "enable_auto_git_push",
        "enable_auto_merge",
        "enable_comment_based_upgrades",
        "auto_apply_config_suggestions",
        "dry_run",
        # Destination and behaviour selectors.
        "default_submolt",
        "post_after_self_question",
    ],
)
def test_operator_only_fields_are_refused(key):
    safe, reason = _reject_reason({key: "x"})
    assert safe == {}
    assert "operator" in reason


def test_an_unknown_field_is_refused():
    """A deny-list would have accepted anything it had not heard of."""
    safe, reason = _reject_reason({"some_field_added_next_year": 1})
    assert safe == {}
    assert "operator" in reason


def test_every_field_outside_the_allowlist_is_refused():
    """The allowlist is the whole surface, checked against the real dataclass."""
    from dataclasses import fields

    from ouroboros.config_schema import COMMENT_SUGGESTIBLE_FIELDS
    from ouroboros.moltbook import RunnerConfig

    everything = {f.name: 1 for f in fields(RunnerConfig)}
    safe, _ = filter_untrusted_config_updates(everything, {})

    assert set(safe) <= COMMENT_SUGGESTIBLE_FIELDS


def test_a_tuning_field_is_accepted():
    safe, rejected = filter_untrusted_config_updates(
        {"min_post_interval_hours": 24}, {"min_post_interval_hours": 12}
    )
    assert safe == {"min_post_interval_hours": 24}
    assert rejected == []


def test_a_mixed_suggestion_keeps_only_the_allowed_part():
    safe, rejected = filter_untrusted_config_updates(
        {"min_post_interval_hours": 24, "reviewer_base_url": "https://attacker/v1"},
        {"min_post_interval_hours": 12},
    )
    assert safe == {"min_post_interval_hours": 24}
    assert len(rejected) == 1


# -- suggestions may only reduce activity ------------------------------------

def test_an_interval_may_be_increased_by_a_suggestion():
    safe, _ = filter_untrusted_config_updates(
        {"interval_seconds": 3600}, {"interval_seconds": 1800}
    )
    assert safe == {"interval_seconds": 3600}


def test_an_interval_may_not_be_decreased_by_a_suggestion():
    """"Poll ten times faster" is exactly what a stranger must not be able to
    ask for; the bounds alone would have allowed it."""
    safe, reason = _reject_reason(
        {"interval_seconds": 60}, {"interval_seconds": 1800}
    )
    assert safe == {}
    assert "only be increased" in reason


def test_a_count_may_be_reduced_by_a_suggestion():
    safe, _ = filter_untrusted_config_updates(
        {"max_comments_per_cycle": 1}, {"max_comments_per_cycle": 3}
    )
    assert safe == {"max_comments_per_cycle": 1}


def test_a_count_may_not_be_raised_by_a_suggestion():
    safe, reason = _reject_reason(
        {"max_comments_per_cycle": 50}, {"max_comments_per_cycle": 3}
    )
    assert safe == {}
    assert "only be reduced" in reason


# -- bounds ------------------------------------------------------------------

@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("interval_seconds", 0),           # busy loop
        ("interval_seconds", -1),
        ("interval_seconds", 10**12),      # parks the agent for millennia
        ("max_comments_per_cycle", -1),
        ("max_comments_per_cycle", 10_000),
        ("interval_seconds", "not a number"),
        ("interval_seconds", 1.5),
        ("interval_seconds", True),        # bool is an int subclass
    ],
)
def test_out_of_range_and_wrong_typed_suggestions_are_refused(key, value):
    safe, _ = filter_untrusted_config_updates({key: value}, {key: 1800})
    assert safe == {}
