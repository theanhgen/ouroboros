import json
import os

import pytest
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from ouroboros.moltbook import (
    Credentials,
    MAX_SELF_QUESTION_LOG,
    MoltbookError,
    RunnerConfig,
    _trim_self_question_log,
    load_credentials,
    load_runner_config,
    load_state,
    run_loop,
    save_state,
)
from ouroboros.model_defaults import DEFAULT_OPENAI_MODEL


def test_load_credentials_from_env():
    with mock.patch.dict(os.environ, {"MOLTBOOK_API_KEY": "k", "MOLTBOOK_AGENT_NAME": "a"}):
        creds = load_credentials()
    assert creds == Credentials(api_key="k", agent_name="a")


def test_load_credentials_from_file(tmp_path):
    cred_file = tmp_path / "credentials.json"
    cred_file.write_text(json.dumps({"api_key": "fk", "agent_name": "fa"}))

    with mock.patch.dict(os.environ, {}, clear=True):
        with mock.patch("ouroboros.moltbook.os.path.expanduser", return_value=str(cred_file)):
            # Also patch os.path.exists to match the patched expanduser path
            orig_exists = os.path.exists
            def fake_exists(p):
                if p == str(cred_file):
                    return True
                return orig_exists(p)

            with mock.patch("ouroboros.moltbook.os.path.exists", side_effect=fake_exists):
                creds = load_credentials()

    assert creds.api_key == "fk"
    assert creds.agent_name == "fa"


def test_load_credentials_without_agent_name_when_not_required(tmp_path):
    cred_file = tmp_path / "credentials.json"
    cred_file.write_text(json.dumps({"api_key": "fk"}))

    with mock.patch.dict(os.environ, {}, clear=True):
        with mock.patch("ouroboros.moltbook.os.path.expanduser", return_value=str(cred_file)):
            orig_exists = os.path.exists

            def fake_exists(p):
                if p == str(cred_file):
                    return True
                return orig_exists(p)

            with mock.patch("ouroboros.moltbook.os.path.exists", side_effect=fake_exists):
                creds = load_credentials(require_agent_name=False)

    assert creds.api_key == "fk"
    assert creds.agent_name == ""


def test_runner_config_defaults():
    cfg = RunnerConfig()
    assert cfg.interval_seconds == 1800
    assert cfg.dry_run is False
    assert cfg.enable_auto_git_push is False
    assert cfg.enable_self_improvement_in_loop is True
    assert cfg.self_improvement_retry_minutes == 60
    assert cfg.enable_auto_issue_creation is True
    assert cfg.max_comments_per_cycle == 3
    assert cfg.min_comment_interval_seconds == 300
    assert cfg.enable_auto_comment is False
    assert cfg.enable_self_improvement is False
    assert cfg.improvement_interval_hours == 48
    assert cfg.improvement_model == DEFAULT_OPENAI_MODEL
    assert cfg.enable_auto_merge is False
    assert cfg.enable_issue_scouting is False
    assert cfg.issue_scouting_interval_hours == 24
    assert cfg.issue_scouting_model == DEFAULT_OPENAI_MODEL


def test_load_runner_config_from_file(tmp_path):
    cfg_file = tmp_path / "agent.json"
    cred_file = tmp_path / "credentials.json"
    cfg_file.write_text(json.dumps({
        "interval_seconds": 60,
        "dry_run": False,
        "max_comments_per_cycle": 5,
        "min_comment_interval_seconds": 120
    }))

    def fake_expanduser(path):
        if path == "~/.config/moltbook/agent.json":
            return str(cfg_file)
        if path == "~/.config/moltbook/credentials.json":
            return str(cred_file)
        return path

    orig_exists = os.path.exists

    def fake_exists(path):
        if os.fspath(path) == str(cfg_file):
            return True
        return orig_exists(path)

    with mock.patch("ouroboros.moltbook.os.path.expanduser", side_effect=fake_expanduser):
        with mock.patch("ouroboros.moltbook.os.path.exists", side_effect=fake_exists):
            cfg = load_runner_config()

    assert cfg.interval_seconds == 60
    assert cfg.dry_run is False
    assert cfg.max_comments_per_cycle == 5
    assert cfg.min_comment_interval_seconds == 120
    assert cfg.telegram_bot_token is None


def test_load_runner_config_uses_legacy_self_improve_interval(tmp_path):
    cfg_file = tmp_path / "agent.json"
    cfg_file.write_text(json.dumps({
        "self_improve_interval_hours": 12,
    }))

    def fake_expanduser(path):
        if path == "~/.config/moltbook/agent.json":
            return str(cfg_file)
        return path

    orig_exists = os.path.exists

    def fake_exists(path):
        if os.fspath(path) == str(cfg_file):
            return True
        return orig_exists(path)

    with mock.patch("ouroboros.moltbook.os.path.expanduser", side_effect=fake_expanduser):
        with mock.patch("ouroboros.moltbook.os.path.exists", side_effect=fake_exists):
            cfg = load_runner_config()

    assert cfg.improvement_interval_hours == 12
    assert cfg.self_improve_interval_hours == 12


def test_load_runner_config_telegram_from_credentials(tmp_path):
    cfg_file = tmp_path / "agent.json"
    cred_file = tmp_path / "credentials.json"
    cfg_file.write_text(json.dumps({
        "enable_telegram_notifications": True,
        "telegram_chat_id": "from-agent",
    }))
    cred_file.write_text(json.dumps({
        "telegram_bot_token": "from-credentials",
        "telegram_chat_id": "from-credentials",
    }))

    def fake_expanduser(path):
        if path == "~/.config/moltbook/agent.json":
            return str(cfg_file)
        if path == "~/.config/moltbook/credentials.json":
            return str(cred_file)
        return path

    orig_exists = os.path.exists

    def fake_exists(path):
        if os.fspath(path) in {str(cfg_file), str(cred_file)}:
            return True
        return orig_exists(path)

    with mock.patch.dict(os.environ, {}, clear=True):
        with mock.patch("ouroboros.moltbook.os.path.expanduser", side_effect=fake_expanduser):
            with mock.patch("ouroboros.moltbook.os.path.exists", side_effect=fake_exists):
                cfg = load_runner_config()

    assert cfg.telegram_bot_token == "from-credentials"
    assert cfg.telegram_chat_id == "from-credentials"


def test_load_runner_config_missing_file():
    with mock.patch("ouroboros.moltbook.os.path.exists", return_value=False):
        cfg = load_runner_config()
    assert cfg == RunnerConfig()


def test_load_state_default():
    with mock.patch("ouroboros.moltbook.os.path.exists", return_value=False):
        state = load_state()
    assert state["last_check"] is None
    assert state["last_comment_time"] is None
    assert state["self_question_index"] == 0
    assert state["seen_post_ids"] == []
    assert state["last_issue_scouting_attempt"] is None


def test_save_and_load_state(tmp_path):
    state_file = tmp_path / "state.json"
    with mock.patch("ouroboros.moltbook._state_path", return_value=str(state_file)):
        save_state({"last_check": 123, "seen_post_ids": ["a"]})

    with mock.patch("ouroboros.moltbook._state_path", return_value=str(state_file)):
        with mock.patch("ouroboros.moltbook.os.path.exists", return_value=True):
            loaded = load_state()

    assert loaded["last_check"] == 123
    assert loaded["seen_post_ids"] == ["a"]


def test_trim_self_question_log_under_limit():
    state = {"self_question_log": [{"q": i} for i in range(10)]}
    _trim_self_question_log(state)
    assert len(state["self_question_log"]) == 10


def test_trim_self_question_log_over_limit():
    entries = [{"q": i} for i in range(MAX_SELF_QUESTION_LOG + 50)]
    state = {"self_question_log": entries}
    _trim_self_question_log(state)
    assert len(state["self_question_log"]) == MAX_SELF_QUESTION_LOG
    # Keeps the most recent entries
    assert state["self_question_log"][0] == {"q": 50}


def test_save_state_atomic_write(tmp_path):
    """save_state writes to .tmp then renames -- no partial writes."""
    state_file = tmp_path / "state.json"
    with mock.patch("ouroboros.moltbook._state_path", return_value=str(state_file)):
        save_state({"key": "value"})

    assert state_file.exists()
    assert not (tmp_path / "state.json.tmp").exists()
    loaded = json.loads(state_file.read_text())
    assert loaded["key"] == "value"


def test_run_loop_without_moltbook_credentials_skips_feed_and_runs_github_cycle():
    temp_event = threading.Event()
    now = int(time.time())
    state = {
        "last_self_question": now,
        "last_memory_hygiene": now,
    }
    cfg = RunnerConfig(
        interval_seconds=1,
        enable_github_improvement=True,
        github_improvement_interval_hours=0,
    )

    with mock.patch("ouroboros.moltbook._shutdown_event", temp_event):
        with mock.patch("ouroboros.moltbook.signal.signal"):
            with mock.patch(
                "ouroboros.moltbook.load_credentials",
                side_effect=MoltbookError("Missing API key. Set MOLTBOOK_API_KEY or credentials.json"),
            ):
                with mock.patch("ouroboros.moltbook.load_runner_config", return_value=cfg):
                    with mock.patch("ouroboros.moltbook.load_state", return_value=state):
                        with mock.patch("ouroboros.llm.load_openai_key", return_value="sk-test"):
                            with mock.patch("ouroboros.llm.make_client", return_value=mock.sentinel.client):
                                with mock.patch("ouroboros.moltbook.get_status") as mock_status:
                                    with mock.patch("ouroboros.moltbook.get_feed") as mock_feed:
                                        with mock.patch("ouroboros.moltbook._notify"):
                                            with mock.patch("ouroboros.moltbook.save_state"):
                                                with mock.patch("ouroboros.git_ops.pull_latest", return_value=False):
                                                    with mock.patch(
                                                        "ouroboros.github_improvement.run_github_improvement_cycle",
                                                        return_value=[],
                                                    ) as mock_github_cycle:
                                                        with mock.patch(
                                                            "ouroboros.codebase.get_repo_root",
                                                            return_value=Path("/tmp"),
                                                        ):
                                                            with mock.patch(
                                                                "ouroboros.moltbook._interruptible_sleep",
                                                                side_effect=lambda seconds, check_git=False: temp_event.set(),
                                                            ):
                                                                result = run_loop()

    assert result == 0
    mock_status.assert_not_called()
    mock_feed.assert_not_called()
    mock_github_cycle.assert_called_once()


def test_run_loop_without_moltbook_credentials_runs_issue_scouting():
    temp_event = threading.Event()
    now = int(time.time())
    state = {
        "last_self_question": now,
        "last_memory_hygiene": now,
    }
    cfg = RunnerConfig(
        interval_seconds=1,
        enable_issue_scouting=True,
        issue_scouting_interval_hours=0,
    )

    with mock.patch("ouroboros.moltbook._shutdown_event", temp_event):
        with mock.patch("ouroboros.moltbook.signal.signal"):
            with mock.patch(
                "ouroboros.moltbook.load_credentials",
                side_effect=MoltbookError("Missing API key. Set MOLTBOOK_API_KEY or credentials.json"),
            ):
                with mock.patch("ouroboros.moltbook.load_runner_config", return_value=cfg):
                    with mock.patch("ouroboros.moltbook.load_state", return_value=state):
                        with mock.patch("ouroboros.llm.load_openai_key", return_value="sk-test"):
                            with mock.patch("ouroboros.llm.make_client", return_value=mock.sentinel.client):
                                with mock.patch("ouroboros.moltbook.get_status") as mock_status:
                                    with mock.patch("ouroboros.moltbook.get_feed") as mock_feed:
                                        with mock.patch("ouroboros.moltbook._notify"):
                                            with mock.patch("ouroboros.moltbook.save_state"):
                                                with mock.patch("ouroboros.git_ops.pull_latest", return_value=False):
                                                    with mock.patch(
                                                        "ouroboros.issue_scouting.run_issue_scouting_cycle",
                                                        return_value=mock.MagicMock(
                                                            status="idle",
                                                            message="No improvements identified.",
                                                            task=None,
                                                            issue_url=None,
                                                        ),
                                                    ) as mock_issue_scout:
                                                        with mock.patch(
                                                            "ouroboros.moltbook._interruptible_sleep",
                                                            side_effect=lambda seconds, check_git=False: temp_event.set(),
                                                        ):
                                                            result = run_loop()

    assert result == 0
    mock_status.assert_not_called()
    mock_feed.assert_not_called()
    mock_issue_scout.assert_called_once()


def _runner_config_from(tmp_path, agent_data, cred_data=None):
    cfg_file = tmp_path / "agent.json"
    cred_file = tmp_path / "credentials.json"
    cfg_file.write_text(json.dumps(agent_data))
    if cred_data is not None:
        cred_file.write_text(json.dumps(cred_data))

    def fake_expanduser(path):
        if path == "~/.config/moltbook/agent.json":
            return str(cfg_file)
        if path == "~/.config/moltbook/credentials.json":
            return str(cred_file)
        return path

    orig_exists = os.path.exists

    def fake_exists(path):
        if os.fspath(path) in {str(cfg_file), str(cred_file)}:
            return orig_exists(path)
        return orig_exists(path)

    with mock.patch.dict(os.environ, {}, clear=True):
        with mock.patch("ouroboros.moltbook.os.path.expanduser", side_effect=fake_expanduser):
            with mock.patch("ouroboros.moltbook.os.path.exists", side_effect=fake_exists):
                return load_runner_config()


def test_runner_config_reviewer_defaults():
    cfg = RunnerConfig()
    assert cfg.reviewer_model == ""
    assert cfg.reviewer_base_url == ""
    assert cfg.reviewer_api_key is None


def test_load_runner_config_reads_reviewer_routing(tmp_path):
    cfg = _runner_config_from(
        tmp_path,
        {
            "improvement_model": "gpt-generation",
            "reviewer_model": "qwen3-coder:480b-cloud",
            "reviewer_base_url": "https://ollama.com/v1",
        },
        {"ollama_api_key": "secret"},
    )

    assert cfg.improvement_model == "gpt-generation"
    assert cfg.reviewer_model == "qwen3-coder:480b-cloud"
    assert cfg.reviewer_base_url == "https://ollama.com/v1"
    assert cfg.reviewer_api_key == "secret"


def test_load_runner_config_prefers_explicit_reviewer_api_key(tmp_path):
    cfg = _runner_config_from(
        tmp_path,
        {},
        {"reviewer_api_key": "explicit", "ollama_api_key": "fallback"},
    )
    assert cfg.reviewer_api_key == "explicit"


def test_load_runner_config_reviewer_key_absent_when_unset(tmp_path):
    cfg = _runner_config_from(tmp_path, {}, {})
    assert cfg.reviewer_api_key is None


def test_load_runner_config_reviewer_model_unset_does_not_track_improvement_model(tmp_path):
    """Review must not silently follow the generation model."""
    from ouroboros.config import reviewer_safety_kwargs

    cfg = _runner_config_from(tmp_path, {"improvement_model": "gpt-generation"})

    assert cfg.reviewer_model == ""
    assert "reviewer_model" not in reviewer_safety_kwargs(cfg)


@pytest.mark.parametrize("value", [None, ""])
def test_load_runner_config_null_reviewer_fields_are_empty(tmp_path, value):
    """JSON null must not become the string "None"."""
    cfg = _runner_config_from(
        tmp_path, {"reviewer_model": value, "reviewer_base_url": value}
    )
    assert cfg.reviewer_model == ""
    assert cfg.reviewer_base_url == ""


def test_runner_config_repr_hides_the_reviewer_api_key():
    cfg = RunnerConfig(reviewer_api_key="super-secret")
    assert "super-secret" not in repr(cfg)
