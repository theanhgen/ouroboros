import json
import os
import tempfile
from unittest import mock

from ouroboros.moltbook import (
    Credentials,
    MAX_SELF_QUESTION_LOG,
    RunnerConfig,
    _trim_self_question_log,
    load_credentials,
    load_runner_config,
    load_state,
    save_state,
)


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


def test_runner_config_defaults():
    cfg = RunnerConfig()
    assert cfg.interval_seconds == 1800
    assert cfg.dry_run is True
    assert cfg.max_comments_per_cycle == 3
    assert cfg.min_comment_interval_seconds == 300
    assert cfg.enable_auto_comment is False


def test_load_runner_config_from_file(tmp_path):
    cfg_file = tmp_path / "agent.json"
    cfg_file.write_text(json.dumps({
        "interval_seconds": 60,
        "dry_run": False,
        "max_comments_per_cycle": 5,
        "min_comment_interval_seconds": 120,
    }))

    with mock.patch("ouroboros.moltbook.os.path.expanduser", return_value=str(cfg_file)):
        with mock.patch("ouroboros.moltbook.os.path.exists", return_value=True):
            cfg = load_runner_config()

    assert cfg.interval_seconds == 60
    assert cfg.dry_run is False
    assert cfg.max_comments_per_cycle == 5
    assert cfg.min_comment_interval_seconds == 120


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
