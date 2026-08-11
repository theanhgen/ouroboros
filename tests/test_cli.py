import pytest
import argparse
import json
from unittest.mock import MagicMock, patch
from ouroboros.cli import (
    cmd_apply,
    cmd_backlog_list,
    cmd_improve_identify,
    cmd_improve_status,
    cmd_plan,
    cmd_propose,
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
    args = argparse.Namespace(updates=[
        "interval_seconds=60", "enable_auto_merge=true", "reviewer_model=gpt-4o",
    ])
    ret = cmd_config_modify(args)
    assert ret == 0
    mock_modify.assert_called_once_with({
        "interval_seconds": 60,
        "enable_auto_merge": True,
        "reviewer_model": "gpt-4o",
    })
    captured = capsys.readouterr()
    assert "Updated runner config" in captured.out

@patch("ouroboros.cli.can_self_modify")
def test_cmd_config_modify_disabled(mock_can, capsys):
    mock_can.return_value = False
    args = argparse.Namespace(updates=["interval_seconds=30"])
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


# -- cmd_propose / cmd_apply -------------------------------------------------

def test_cmd_propose_succeeds_under_pr_only(capsys):
    assert cmd_propose(argparse.Namespace()) == 0
    assert "Proposal stub" in capsys.readouterr().out


def test_cmd_propose_calls_the_real_pr_only_validator():
    """The guard must actually run, with the real implementation."""
    with patch("ouroboros.cli.require_pr_only") as guard:
        cmd_propose(argparse.Namespace())
    guard.assert_called_once_with(True)


def test_cmd_propose_skips_the_guard_when_pr_only_is_off(capsys):
    """Documents current behaviour: the check is conditional on the flag it
    checks, so pr_only=False simply bypasses it rather than being rejected."""
    from ouroboros.config import SafetyConfig

    with patch("ouroboros.cli.SafetyConfig", return_value=SafetyConfig(pr_only=False)):
        with patch("ouroboros.cli.require_pr_only") as guard:
            assert cmd_propose(argparse.Namespace()) == 0
    guard.assert_not_called()


def test_cmd_apply_opens_a_pr_by_default(capsys):
    assert cmd_apply(argparse.Namespace()) == 0
    assert "would open PR" in capsys.readouterr().out


def test_cmd_apply_blocked_when_human_approval_required(capsys):
    from ouroboros.config import SafetyConfig

    with patch("ouroboros.cli.SafetyConfig",
               return_value=SafetyConfig(require_human_approval=True)):
        assert cmd_apply(argparse.Namespace()) == 2
    assert "human approval required" in capsys.readouterr().out


def test_cmd_apply_blocked_when_direct_writes_disabled(capsys):
    from ouroboros.config import SafetyConfig

    with patch("ouroboros.cli.SafetyConfig",
               return_value=SafetyConfig(pr_only=False, allow_write_default_branch=False)):
        assert cmd_apply(argparse.Namespace()) == 2
    assert "direct writes disabled" in capsys.readouterr().out


def test_cmd_apply_allows_direct_write_when_configured(capsys):
    from ouroboros.config import SafetyConfig

    with patch("ouroboros.cli.SafetyConfig",
               return_value=SafetyConfig(pr_only=False, allow_write_default_branch=True)):
        assert cmd_apply(argparse.Namespace()) == 0
    assert "default branch" in capsys.readouterr().out


# -- cmd_improve_identify ----------------------------------------------------

@patch("ouroboros.improvement.run_improvement_cycle")
@patch("ouroboros.llm.make_client")
@patch("ouroboros.llm.load_openai_key", return_value="sk-test")
def test_cmd_improve_identify_reports_a_task(_key, _client, mock_cycle, capsys):
    from ouroboros.improvement import ImprovementResult, ImprovementTask

    task = ImprovementTask("id", "fix_bug", "Repair the thing",
                           ["src/ouroboros/x.py"], "a failing test")
    mock_cycle.return_value = ImprovementResult(task=task, status="success")

    assert cmd_improve_identify(argparse.Namespace(model="gpt-test")) == 0

    out = capsys.readouterr().out
    assert "[fix_bug] Repair the thing" in out
    assert "src/ouroboros/x.py" in out
    assert "a failing test" in out


@patch("ouroboros.improvement.run_improvement_cycle", return_value=None)
@patch("ouroboros.llm.make_client")
@patch("ouroboros.llm.load_openai_key", return_value="sk-test")
def test_cmd_improve_identify_when_nothing_found(_key, _client, _cycle, capsys):
    assert cmd_improve_identify(argparse.Namespace(model="gpt-test")) == 0
    assert "No improvements identified." in capsys.readouterr().out


@patch("ouroboros.improvement.run_improvement_cycle", return_value=None)
@patch("ouroboros.llm.make_client")
@patch("ouroboros.llm.load_openai_key", return_value="sk-test")
def test_cmd_improve_identify_is_a_dry_run(_key, _client, mock_cycle):
    """It must never actually change anything."""
    cmd_improve_identify(argparse.Namespace(model="gpt-test"))
    assert mock_cycle.call_args.kwargs["dry_run"] is True


# -- cmd_backlog_list --------------------------------------------------------

@patch("ouroboros.backlog.get_pending", return_value=[])
@patch("ouroboros.codebase.get_repo_root", return_value="/repo")
def test_cmd_backlog_list_empty(_root, _pending, capsys):
    assert cmd_backlog_list(argparse.Namespace()) == 0
    assert "Backlog is empty." in capsys.readouterr().out


@patch("ouroboros.backlog.get_pending")
@patch("ouroboros.codebase.get_repo_root", return_value="/repo")
def test_cmd_backlog_list_prints_each_item(_root, mock_pending, capsys):
    mock_pending.return_value = [
        {"id": "a1", "priority": 1, "task_type": "fix_bug", "description": "First"},
        {"id": "b2", "priority": 3, "task_type": "add_test", "description": "Second"},
    ]

    assert cmd_backlog_list(argparse.Namespace()) == 0

    out = capsys.readouterr().out
    assert "[a1] P1 fix_bug: First" in out
    assert "[b2] P3 add_test: Second" in out


# -- cmd_config_modify: parsing and the error path ---------------------------

@pytest.mark.parametrize("bad", ["novalue", "", "just-a-key"])
@patch("ouroboros.cli.can_self_modify", return_value=True)
@patch("ouroboros.cli.modify_runner_config")
def test_cmd_config_modify_rejects_malformed_updates(mock_modify, _can, bad, capsys):
    """A malformed pair must abort before anything is written."""
    assert cmd_config_modify(argparse.Namespace(updates=[bad])) == 1

    assert "Invalid update format" in capsys.readouterr().out
    mock_modify.assert_not_called()


@patch("ouroboros.cli.can_self_modify", return_value=True)
@patch("ouroboros.cli.modify_runner_config")
def test_cmd_config_modify_rejects_before_applying_earlier_valid_pairs(
    mock_modify, _can
):
    """The bad pair is second; nothing should be written at all."""
    assert cmd_config_modify(
        argparse.Namespace(updates=["interval_seconds=60", "broken"])
    ) == 1
    mock_modify.assert_not_called()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("enable_auto_merge=true", {"enable_auto_merge": True}),
        ("enable_auto_merge=True", {"enable_auto_merge": True}),
        ("enable_auto_merge=false", {"enable_auto_merge": False}),
        ("enable_auto_merge=off", {"enable_auto_merge": False}),
        ("interval_seconds=42", {"interval_seconds": 42}),
        ("reviewer_model=gpt-4o", {"reviewer_model": "gpt-4o"}),
        # Only the first separator splits.
        ("reviewer_base_url=https://x/v1?a=b",
         {"reviewer_base_url": "https://x/v1?a=b"}),
        ("improvement_types=fix_bug, add_test",
         {"improvement_types": ["fix_bug", "add_test"]}),
    ],
)
@patch("ouroboros.cli.can_self_modify", return_value=True)
@patch("ouroboros.cli.modify_runner_config")
def test_cmd_config_modify_value_parsing(mock_modify, _can, raw, expected):
    """The type comes from the field, not from guessing at the text."""
    assert cmd_config_modify(argparse.Namespace(updates=[raw])) == 0
    assert mock_modify.call_args.args[0] == expected


@pytest.mark.parametrize(
    "secret_key",
    ["telegram_bot_token", "telegram_chat_id", "reviewer_api_key", "ollama_api_key"],
)
@patch("ouroboros.cli.can_self_modify", return_value=True)
@patch("ouroboros.cli.modify_runner_config")
def test_cmd_config_modify_redacts_every_secret(mock_modify, _can, secret_key, capsys):
    """The value still has to reach modify_runner_config, just not the console
    -- which ends up in shell history and CI logs."""
    assert cmd_config_modify(
        argparse.Namespace(updates=[f"{secret_key}=super-secret",
                                    "interval_seconds=60"])
    ) == 0

    out = capsys.readouterr().out
    assert "super-secret" not in out
    assert "***" in out
    assert "interval_seconds" in out
    assert mock_modify.call_args.args[0][secret_key] == "super-secret"


# -- config-modify: values that would break the running agent ----------------

@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("interval_seconds=0.5", "int(0.5) is 0 -- a loop that never sleeps"),
        ("interval_seconds=1.5", "int('1.5') raises in the loader"),
        ("interval_seconds=-1", "negative sleep"),
        ("interval_seconds=0", "zero sleep"),
        ("interval_seconds=", "int('') raises in the loader"),
        ("interval_seconds=   ", "whitespace is still empty"),
        ("interval_seconds=NaN", "not a whole number"),
        ("interval_seconds=1e309", "not a whole number"),
        ("interval_seconds=abc", "not a whole number"),
    ],
)
@patch("ouroboros.cli.can_self_modify", return_value=True)
@patch("ouroboros.cli.modify_runner_config")
def test_cmd_config_modify_rejects_values_that_break_the_agent(
    mock_modify, _can, raw, why, capsys
):
    """These either stop the agent starting or remove its rate limiting."""
    assert cmd_config_modify(argparse.Namespace(updates=[raw])) == 1, why

    assert "Invalid update" in capsys.readouterr().out
    mock_modify.assert_not_called()


@pytest.mark.parametrize(
    "key", ["reviewer_base_url", "reviewer_model", "generator_model",
            "telegram_bot_token", "reviewer_api_key"],
)
@patch("ouroboros.cli.can_self_modify", return_value=True)
@patch("ouroboros.cli.modify_runner_config")
def test_cmd_config_modify_allows_clearing_string_fields(mock_modify, _can, key):
    """Empty means "unset" for these, and clearing a compromised reviewer
    endpoint or credential has to be possible from the CLI."""
    assert cmd_config_modify(argparse.Namespace(updates=[f"{key}="])) == 0
    assert mock_modify.call_args.args[0] == {key: ""}


@patch("ouroboros.cli.can_self_modify", return_value=True)
@patch("ouroboros.cli.modify_runner_config")
def test_cmd_config_modify_accepts_a_valid_interval(mock_modify, _can):
    assert cmd_config_modify(argparse.Namespace(updates=["interval_seconds=60"])) == 0
    assert mock_modify.call_args.args[0] == {"interval_seconds": 60}


def test_the_schema_is_derived_from_the_dataclass():
    """Hardcoding the list would leave a new field unvalidated."""
    from ouroboros import config_schema

    types = config_schema.field_types()
    assert "interval_seconds" in types
    assert "improvement_interval_hours" in types
    assert "reviewer_model" in types
    assert "enable_auto_merge" in types


def test_a_valid_interval_survives_the_round_trip_to_the_loader():
    """The real check: what the CLI writes must be what the loader can read."""
    from ouroboros.config_schema import parse_value
    from ouroboros.moltbook import RunnerConfig

    written, error = parse_value("interval_seconds", "60")
    assert error is None
    assert RunnerConfig(interval_seconds=int(written)).interval_seconds == 60


@patch("ouroboros.llm.make_client")
@patch("ouroboros.llm.load_openai_key", return_value="sk-test")
@patch("ouroboros.codebase.get_repo_root", return_value="/repo")
@patch("ouroboros.backlog.organize_backlog")
def test_cmd_backlog_organize_reports_failure(mock_org, _root, _key, _client, capsys):
    """It used to print "Done." whether the organizer had succeeded or been
    rejected outright."""
    from ouroboros.backlog import OrganizeResult
    from ouroboros.cli import cmd_backlog_clean

    mock_org.return_value = OrganizeResult(ok=False, reason="no decision for: b2")

    assert cmd_backlog_clean(argparse.Namespace()) == 1
    assert "no decision for: b2" in capsys.readouterr().out


@patch("ouroboros.llm.make_client")
@patch("ouroboros.llm.load_openai_key", return_value="sk-test")
@patch("ouroboros.codebase.get_repo_root", return_value="/repo")
@patch("ouroboros.backlog.organize_backlog")
def test_cmd_backlog_organize_reports_success(mock_org, _root, _key, _client, capsys):
    from ouroboros.backlog import OrganizeResult
    from ouroboros.cli import cmd_backlog_clean

    mock_org.return_value = OrganizeResult(ok=True, kept=3, deleted=1, merged=0)

    assert cmd_backlog_clean(argparse.Namespace()) == 0
    assert "3 kept" in capsys.readouterr().out


# -- config_schema -----------------------------------------------------------

def test_unknown_key_suggests_a_near_match():
    from ouroboros.config_schema import validate

    error = validate("interval_secnds", 60)
    assert "unknown setting" in error
    assert "interval_seconds" in error, "a silently ignored typo is the worst outcome"


def test_a_typo_is_rejected_rather_than_silently_stored():
    from ouroboros.config_schema import validate

    assert validate("enabel_auto_merge", True) is not None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("interval_seconds", 1800),
        ("max_comments_per_cycle", 0),
        ("oddities_digest_hour", 0),
        ("oddities_digest_hour", 23),
        ("enable_auto_merge", True),
        ("reviewer_model", "gpt-4o"),
        ("reviewer_base_url", ""),
        ("improvement_types", ["fix_bug"]),
    ],
)
def test_valid_settings_pass(key, value):
    from ouroboros.config_schema import validate

    assert validate(key, value) is None


@pytest.mark.parametrize(
    ("key", "value", "why"),
    [
        ("interval_seconds", 0, "busy loop"),
        ("interval_seconds", -1, "negative sleep"),
        ("interval_seconds", 10**12, "parks the agent for millennia"),
        ("oddities_digest_hour", 24, "not an hour of the day"),
        ("oddities_digest_hour", -1, "not an hour of the day"),
        ("max_comments_per_cycle", -1, "negative count"),
        ("interval_seconds", True, "bool is an int subclass"),
        ("interval_seconds", "1800", "a string for a numeric field"),
        ("enable_auto_merge", 1, "an int for a boolean field"),
        ("enable_auto_merge", "yes", "a string for a boolean field"),
    ],
)
def test_invalid_settings_are_rejected(key, value, why):
    from ouroboros.config_schema import validate

    assert validate(key, value) is not None, why


def test_every_int_field_has_a_positive_floor():
    """A missing bounds entry must not mean "anything goes"."""
    from dataclasses import fields

    from ouroboros.config_schema import validate
    from ouroboros.moltbook import RunnerConfig

    for field in fields(RunnerConfig):
        if field.type in ("int", int):
            assert validate(field.name, -1) is not None, field.name


def test_every_default_is_itself_valid():
    """The shipped defaults must satisfy the schema that guards them."""
    from dataclasses import fields

    from ouroboros.config_schema import validate
    from ouroboros.moltbook import RunnerConfig

    cfg = RunnerConfig()
    for field in fields(RunnerConfig):
        value = getattr(cfg, field.name)
        assert validate(field.name, value) is None, f"{field.name}={value!r}"


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("interval_seconds", "60", 60),
        ("enable_auto_merge", "true", True),
        ("enable_auto_merge", "off", False),
        ("reviewer_model", "gpt-4o", "gpt-4o"),
        ("reviewer_base_url", "", ""),
        ("improvement_types", "fix_bug, add_test", ["fix_bug", "add_test"]),
        ("improvement_types", "", None),
    ],
)
def test_parse_value_uses_the_declared_type(key, raw, expected):
    from ouroboros.config_schema import parse_value

    parsed, error = parse_value(key, raw)
    assert error is None
    assert parsed == expected


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("interval_seconds", "1.5"),
        ("interval_seconds", "abc"),
        ("interval_seconds", ""),
        ("enable_auto_merge", "maybe"),
        ("nonexistent_field", "1"),
    ],
)
def test_parse_value_reports_bad_input(key, raw):
    from ouroboros.config_schema import parse_value

    _parsed, error = parse_value(key, raw)
    assert error is not None


def test_credential_aliases_are_settable():
    """ollama_api_key is not a RunnerConfig field, but config modify routes it
    to credentials.json, so the schema has to know it."""
    from ouroboros.config_schema import validate

    assert validate("ollama_api_key", "secret") is None


def test_every_tracked_config_key_is_a_known_setting():
    """The checked-in config is the operator's worked example.

    A key that lives there but is not a RunnerConfig field is either dead or
    read behind a getattr default -- and the schema would reject an operator
    setting the project itself ships.
    """
    import json
    from pathlib import Path

    from ouroboros.config_schema import field_types, validate

    tracked = Path(__file__).resolve().parents[1] / "config" / "agent.json"
    if not tracked.exists():
        pytest.skip("no tracked sample config")

    known = field_types()
    for key, value in json.loads(tracked.read_text()).items():
        assert key in known, f"{key} is in config/agent.json but not a setting"
        assert validate(key, value) is None, f"{key}={value!r}"


def test_setting_a_superseded_key_is_refused_rather_than_silently_ignored():
    """The loader honours self_improve_interval_hours only when
    improvement_interval_hours is absent, so writing it normally reports
    success and changes nothing."""
    from ouroboros.config_schema import settable_error

    error = settable_error("self_improve_interval_hours")
    assert error and "improvement_interval_hours" in error
    assert settable_error("improvement_interval_hours") is None


def test_a_float_field_parses_and_validates_as_a_float():
    from ouroboros.config_schema import parse_value, validate

    value, error = parse_value("community_post_interval_hours", "0.5")
    assert (value, error) == (0.5, None)
    assert validate("community_post_interval_hours", 0.5) is None
    assert validate("community_post_interval_hours", 0) is not None
    assert validate("community_post_interval_hours", "soon") is not None
