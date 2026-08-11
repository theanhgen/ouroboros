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


# -- a suggestion may not park the agent -------------------------------------

def test_an_interval_may_not_be_raised_past_the_public_ceiling():
    """The direction rule alone makes "less active" unbounded.

    interval_seconds has an operator ceiling of a year, and a year is an
    increase, so the direction rule waves it through -- a comment could stop
    the agent for as long as it liked while every guard reported success.
    """
    safe, reason = _reject_reason(
        {"interval_seconds": 31_536_000}, {"interval_seconds": 1800}
    )
    assert safe == {}
    assert "may not be set above 21600" in reason


def test_error_throttling_may_not_be_raised_to_a_year_by_a_suggestion():
    """Silencing the alerts is how you hide having parked the agent."""
    safe, _ = filter_untrusted_config_updates(
        {"telegram_error_min_interval_seconds": 31_536_000},
        {"telegram_error_min_interval_seconds": 300},
    )
    assert safe == {}


def test_a_suggestion_may_still_throttle_within_the_ceiling():
    safe, _ = filter_untrusted_config_updates(
        {"interval_seconds": 7200}, {"interval_seconds": 1800}
    )
    assert safe == {"interval_seconds": 7200}


# -- direction is declared, not guessed from the name ------------------------

def test_lowering_the_early_analysis_threshold_is_refused():
    """It reads like a capacity count, so a name-suffix heuristic classified it
    as reduce-only -- but lowering it makes the agent analyse *sooner*."""
    safe, reason = _reject_reason(
        {"community_min_comments_for_early": 0},
        {"community_min_comments_for_early": 5},
    )
    assert safe == {}
    assert "only be increased" in reason


def test_raising_the_early_analysis_threshold_is_allowed():
    safe, _ = filter_untrusted_config_updates(
        {"community_min_comments_for_early": 10},
        {"community_min_comments_for_early": 5},
    )
    assert safe == {"community_min_comments_for_early": 10}


# -- fail closed -------------------------------------------------------------

def test_an_unreadable_current_config_rejects_every_suggestion(monkeypatch):
    """Without the current values the direction rule silently passes everything,
    so a decrease that should be refused would be applied."""
    import ouroboros.moltbook as moltbook

    def boom():
        raise OSError("config unreadable")

    monkeypatch.setattr(moltbook, "load_runner_config", boom)

    safe, rejected = filter_untrusted_config_updates({"interval_seconds": 60})
    assert safe == {}
    assert any("unreadable" in r for r in rejected)


def test_a_deprecated_alias_is_not_suggestible():
    safe, _ = filter_untrusted_config_updates(
        {"self_improve_interval_hours": 96}, {"self_improve_interval_hours": 48}
    )
    assert safe == {}


# -- the allowlist cannot drift away from its guards -------------------------

def test_every_suggestible_field_has_a_direction_and_bounds():
    """Opening a field for suggestion without a direction entry gives it no
    direction check at all -- silently, which is how the deny-list this
    replaced went wrong."""
    from ouroboros.config_schema import (
        COMMENT_SUGGESTIBLE_FIELDS,
        _SUGGESTION_BOUNDS,
        _SUGGESTION_DIRECTION,
        field_types,
    )

    known = field_types()
    for name in COMMENT_SUGGESTIBLE_FIELDS:
        assert name in known, f"{name} is suggestible but not a setting"
        assert name in _SUGGESTION_DIRECTION, f"{name} has no declared direction"
        assert name in _SUGGESTION_BOUNDS, f"{name} has no suggestion bounds"


def test_suggestion_guards_do_not_name_fields_that_are_not_suggestible():
    from ouroboros.config_schema import (
        COMMENT_SUGGESTIBLE_FIELDS,
        _SUGGESTION_BOUNDS,
        _SUGGESTION_DIRECTION,
    )

    assert set(_SUGGESTION_DIRECTION) == set(COMMENT_SUGGESTIBLE_FIELDS)
    assert set(_SUGGESTION_BOUNDS) == set(COMMENT_SUGGESTIBLE_FIELDS)


# -- an uncomparable current value must fail, not skip the check -------------

def test_a_missing_current_value_refuses_the_suggestion():
    """isinstance(None, int) is False, so the direction rule used to skip
    rather than fail -- and interval_seconds=60 passed on bounds alone."""
    safe, reason = _reject_reason({"interval_seconds": 60}, {})
    assert safe == {}
    assert "cannot be checked" in reason


def test_a_partial_current_config_does_not_open_the_other_fields():
    safe, _ = filter_untrusted_config_updates(
        {"interval_seconds": 60, "max_comments_per_cycle": 1},
        {"max_comments_per_cycle": 3},
    )
    assert "interval_seconds" not in safe
    assert safe == {"max_comments_per_cycle": 1}


# -- model-shaped values are coerced, not waved through ----------------------

def test_a_string_number_from_a_model_is_coerced_and_still_guarded():
    """The old deny-list passed "60" straight to the loader, which coerced it.
    Coercing here keeps that working *and* makes it comparable -- a str is
    uncomparable, so it would otherwise dodge the direction rule."""
    safe, _ = filter_untrusted_config_updates(
        {"interval_seconds": "3600"}, {"interval_seconds": 1800}
    )
    assert safe == {"interval_seconds": 3600}

    blocked, reason = _reject_reason(
        {"interval_seconds": "60"}, {"interval_seconds": 1800}
    )
    assert blocked == {}
    assert "only be increased" in reason


def test_a_whole_float_from_a_model_is_coerced_and_still_guarded():
    safe, _ = filter_untrusted_config_updates(
        {"interval_seconds": 3600.0}, {"interval_seconds": 1800}
    )
    assert safe == {"interval_seconds": 3600}

    blocked, _ = filter_untrusted_config_updates(
        {"interval_seconds": 60.0}, {"interval_seconds": 1800}
    )
    assert blocked == {}


def test_a_fractional_value_is_still_refused_for_a_whole_number_field():
    safe, _ = filter_untrusted_config_updates(
        {"interval_seconds": 3600.5}, {"interval_seconds": 1800}
    )
    assert safe == {}


# -- throttling may not become silencing -------------------------------------

def test_a_suggestion_may_not_silence_commenting_outright():
    """0 is a legitimate operator setting meaning "do not comment"; a stranger
    reaching it is not a throttle."""
    safe, _ = filter_untrusted_config_updates(
        {"max_comments_per_cycle": 0}, {"max_comments_per_cycle": 3}
    )
    assert safe == {}


def test_a_suggestion_may_not_park_the_channel_it_arrived_through():
    """Comments are how a bad suggestion gets undone."""
    safe, _ = filter_untrusted_config_updates(
        {"comment_check_interval_hours": 168}, {"comment_check_interval_hours": 6}
    )
    assert safe == {}
