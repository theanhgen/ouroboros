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


def test_load_state_default(tmp_path):
    # Point at a path that really is absent rather than mocking os.path.exists:
    # load_state no longer preflights, so the mock would not apply and the test
    # would read whatever real state.json the host happens to have.
    missing = tmp_path / "state.json"
    with mock.patch("ouroboros.moltbook._state_path", return_value=str(missing)):
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


def test_load_state_unreadable_does_not_reset_to_default(tmp_path):
    """A stat/read error must not look like a fresh install.

    run_loop persists what it loaded, so returning defaults here would
    overwrite seen_post_ids and the cycle timestamps and the agent would
    repeat work it had already done.
    """
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_check": 123, "seen_post_ids": ["a"]}))

    real_open = Path.open

    def unreadable(self, *a, **kw):
        if str(self) == str(state_file):
            raise PermissionError("EACCES")
        return real_open(self, *a, **kw)

    with mock.patch("ouroboros.moltbook._state_path", return_value=str(state_file)):
        with mock.patch.object(Path, "open", unreadable):
            with pytest.raises(PermissionError):
                load_state()

    assert json.loads(state_file.read_text())["seen_post_ids"] == ["a"]


# -- feed visibility is not knowledge processing (#67) -----------------------

def _posts(n, start=1):
    return [
        {"id": f"p{i}", "title": f"t{i}", "content": f"c{i}"}
        for i in range(start, start + n)
    ]


def test_posts_beyond_the_batch_budget_stay_queued_rather_than_being_dropped():
    """The bug: with ten posts a fetch, the sixth uncommented one was marked
    seen without ever reaching extraction, and never came back."""
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    added = moltbook._queue_for_extraction(state, _posts(10))
    assert added == 10
    assert len(state["knowledge_pending"]) == 10

    with mock.patch.object(
        llm_mod, "extract_insights_batch",
        return_value=[{"post_index": 0, "insight": "i0", "tags": []}],
    ):
        entries = moltbook._drain_extraction_queue(state, object())

    assert [e["post_id"] for e in entries] == ["p1"]
    # The five that were processed are gone; the other five are still waiting.
    assert [e["id"] for e in state["knowledge_pending"]] == [
        "p6", "p7", "p8", "p9", "p10"
    ]


def test_the_queue_drains_across_cycles_until_empty():
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(12))
    seen_batches = []

    def record(_client, batch, *a, **kw):
        seen_batches.append([p["id"] for p in batch])
        return []

    with mock.patch.object(llm_mod, "extract_insights_batch", side_effect=record):
        for _ in range(3):
            moltbook._drain_extraction_queue(state, object())

    assert seen_batches == [
        ["p1", "p2", "p3", "p4", "p5"],
        ["p6", "p7", "p8", "p9", "p10"],
        ["p11", "p12"],
    ]
    assert state["knowledge_pending"] == []


def test_a_batch_that_found_nothing_still_clears_the_queue():
    """[] means the extraction ran and found nothing -- a decision. Only None
    means it failed."""
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(3))
    with mock.patch.object(llm_mod, "extract_insights_batch", return_value=[]):
        assert moltbook._drain_extraction_queue(state, object()) == []
    assert state["knowledge_pending"] == []


def test_a_failed_extraction_leaves_the_posts_queued():
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(3))
    with mock.patch.object(llm_mod, "extract_insights_batch", return_value=None):
        assert moltbook._drain_extraction_queue(state, object()) == []

    assert [e["id"] for e in state["knowledge_pending"]] == ["p1", "p2", "p3"]
    assert [e["attempts"] for e in state["knowledge_pending"]] == [1, 1, 1]


def test_an_unparseable_reply_counts_as_a_failure_not_a_decision():
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(2))
    with mock.patch.object(llm_mod, "extract_insights_batch", return_value="nonsense"):
        assert moltbook._drain_extraction_queue(state, object()) == []
    assert len(state["knowledge_pending"]) == 2


def test_a_permanently_failing_batch_does_not_block_the_queue_forever():
    """Without an attempt cap the head of a FIFO queue that always fails means
    nothing behind it is ever extracted."""
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(7))

    with mock.patch.object(llm_mod, "extract_insights_batch", return_value=None):
        for _ in range(moltbook.MAX_EXTRACTION_ATTEMPTS):
            moltbook._drain_extraction_queue(state, object())

    # The poisoned head is dropped; what was behind it survives.
    assert [e["id"] for e in state["knowledge_pending"]] == ["p6", "p7"]

    with mock.patch.object(
        llm_mod, "extract_insights_batch",
        return_value=[{"post_index": 1, "insight": "i7", "tags": []}],
    ):
        entries = moltbook._drain_extraction_queue(state, object())
    assert [e["post_id"] for e in entries] == ["p7"]


def test_queueing_the_same_post_twice_does_not_duplicate_it():
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(3))
    assert moltbook._queue_for_extraction(state, _posts(3)) == 0
    assert [e["id"] for e in state["knowledge_pending"]] == ["p1", "p2", "p3"]


def test_the_queue_is_bounded_and_says_what_it_dropped(caplog):
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    with caplog.at_level("WARNING"):
        moltbook._queue_for_extraction(
            state, _posts(moltbook.MAX_KNOWLEDGE_PENDING + 3)
        )
    assert len(state["knowledge_pending"]) == moltbook.MAX_KNOWLEDGE_PENDING
    # Oldest go first, and the drop is named rather than silent.
    assert [e["id"] for e in state["knowledge_pending"][:2]] == ["p4", "p5"]
    assert "dropped 3 oldest" in caplog.text


def test_queued_post_content_is_capped():
    """The queue is persisted with the rest of state; an unbounded copy of
    every post body would bloat it."""
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(
        state, [{"id": "big", "title": "t", "content": "x" * 50_000}]
    )
    assert len(state["knowledge_pending"][0]["content"]) == (
        moltbook.MAX_PENDING_CONTENT_CHARS
    )


def test_a_post_with_no_id_is_not_queued():
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    assert moltbook._queue_for_extraction(state, [{"title": "no id"}]) == 0
    assert state["knowledge_pending"] == []


def test_draining_an_empty_queue_makes_no_llm_call():
    from ouroboros import llm as llm_mod, moltbook

    with mock.patch.object(llm_mod, "extract_insights_batch") as call:
        assert moltbook._drain_extraction_queue({}, object()) == []
    call.assert_not_called()


def test_out_of_range_and_malformed_insight_items_are_ignored():
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(2))
    with mock.patch.object(
        llm_mod, "extract_insights_batch",
        return_value=[
            {"post_index": 99, "insight": "out of range"},
            {"post_index": "nope", "insight": "unparseable index"},
            "not a dict",
            {"post_index": 0, "insight": ""},          # empty insight
            {"post_index": 1, "insight": "kept", "tags": ["t"]},
        ],
    ):
        entries = moltbook._drain_extraction_queue(state, object())

    assert [e["post_id"] for e in entries] == ["p2"]
    assert state["knowledge_pending"] == []


def test_trim_state_keeps_the_most_recent_seen_ids_in_order():
    """The cap is meant to keep recent ids. The old code sliced list(set(...)),
    whose order is arbitrary, so it discarded at random and old posts came
    back as new."""
    from ouroboros import moltbook

    ids = [f"p{i}" for i in range(moltbook.MAX_SEEN_POST_IDS + 25)]
    state = {"seen_post_ids": list(ids)}
    moltbook._trim_state(state)

    assert state["seen_post_ids"] == ids[-moltbook.MAX_SEEN_POST_IDS:]
    assert state["seen_post_ids"][-1] == ids[-1]


def test_trim_state_bounds_the_extraction_queue():
    from ouroboros import moltbook

    state = {
        "knowledge_pending": [
            {"id": f"p{i}", "title": "", "content": "", "attempts": 0}
            for i in range(moltbook.MAX_KNOWLEDGE_PENDING + 10)
        ]
    }
    moltbook._trim_state(state)
    assert len(state["knowledge_pending"]) == moltbook.MAX_KNOWLEDGE_PENDING
    assert state["knowledge_pending"][-1]["id"] == (
        f"p{moltbook.MAX_KNOWLEDGE_PENDING + 9}"
    )
