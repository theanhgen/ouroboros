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
    get_my_posts,
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
        entries, processed = moltbook._drain_extraction_queue(state, object())

    assert [e["post_id"] for e in entries] == ["p1"]
    # Still queued until released -- the queue is not the record.
    assert len(state["knowledge_pending"]) == 10
    moltbook._release_extracted(state, processed)
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
            _, processed = moltbook._drain_extraction_queue(state, object())
            moltbook._release_extracted(state, processed)

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
        entries, processed = moltbook._drain_extraction_queue(state, object())
    assert entries == []
    moltbook._release_extracted(state, processed)
    assert state["knowledge_pending"] == []


def test_a_failed_extraction_leaves_the_posts_queued():
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(3))
    with mock.patch.object(llm_mod, "extract_insights_batch", return_value=None):
        entries, processed = moltbook._drain_extraction_queue(state, object())
    assert (entries, processed) == ([], [])

    assert [e["id"] for e in state["knowledge_pending"]] == ["p1", "p2", "p3"]
    assert [e["attempts"] for e in state["knowledge_pending"]] == [1, 1, 1]


def test_an_unparseable_reply_counts_as_a_failure_not_a_decision():
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(2))
    with mock.patch.object(llm_mod, "extract_insights_batch", return_value="nonsense"):
        assert moltbook._drain_extraction_queue(state, object()) == ([], [])
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
        entries, _ = moltbook._drain_extraction_queue(state, object())
    assert [e["post_id"] for e in entries] == ["p7"]


def test_queueing_the_same_post_twice_does_not_duplicate_it():
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(3))
    assert moltbook._queue_for_extraction(state, _posts(3)) == 0
    assert [e["id"] for e in state["knowledge_pending"]] == ["p1", "p2", "p3"]


def test_the_queue_is_bounded_and_says_what_it_dropped(caplog):
    """The cap is applied at save time, not on enqueue: evicting during
    enqueue dropped the head, which is the batch about to be drained."""
    from ouroboros import moltbook

    state = {}
    moltbook._queue_for_extraction(
        state, _posts(moltbook.MAX_KNOWLEDGE_PENDING + 3)
    )
    # Nothing dropped yet -- this cycle's drain still gets the oldest.
    assert len(state["knowledge_pending"]) == moltbook.MAX_KNOWLEDGE_PENDING + 3

    with caplog.at_level("WARNING"):
        moltbook._trim_state(state)

    assert len(state["knowledge_pending"]) == moltbook.MAX_KNOWLEDGE_PENDING
    assert [e["id"] for e in state["knowledge_pending"][:2]] == ["p4", "p5"]
    assert "dropped 3 oldest" in caplog.text


def test_the_cap_never_evicts_the_batch_about_to_be_drained(caplog):
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(
        state, _posts(moltbook.MAX_KNOWLEDGE_PENDING + 3)
    )
    with mock.patch.object(llm_mod, "extract_insights_batch", return_value=[]):
        _, processed = moltbook._drain_extraction_queue(state, object())

    # The oldest five reached the LLM before any cap was applied.
    assert processed == ["p1", "p2", "p3", "p4", "p5"]


def test_an_exception_from_the_extractor_still_counts_as_an_attempt():
    """The queue's guarantee that it cannot wedge must not depend on the
    extractor's own try/except staying in place."""
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(2))
    with mock.patch.object(
        llm_mod, "extract_insights_batch", side_effect=RuntimeError("boom")
    ):
        assert moltbook._drain_extraction_queue(state, object()) == ([], [])
    assert [e["attempts"] for e in state["knowledge_pending"]] == [1, 1]


def test_release_is_a_no_op_for_an_empty_batch():
    from ouroboros import moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(2))
    moltbook._release_extracted(state, [])
    assert len(state["knowledge_pending"]) == 2


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
        assert moltbook._drain_extraction_queue({}, object()) == ([], [])
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
        entries, processed = moltbook._drain_extraction_queue(state, object())

    assert [e["post_id"] for e in entries] == ["p2"]
    moltbook._release_extracted(state, processed)
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


def test_a_failed_knowledge_write_outboxes_the_entries_and_releases_the_posts():
    """_record_knowledge either writes or queues in the durable outbox, so by
    the time it returns the insight cannot be lost and the post may leave the
    queue. This is the ordering run_loop relies on."""
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(2))
    with mock.patch.object(
        llm_mod, "extract_insights_batch",
        return_value=[{"post_index": 0, "insight": "i1", "tags": []}],
    ):
        entries, processed = moltbook._drain_extraction_queue(state, object())

    seen_on_disk = {}

    def capture(st):
        seen_on_disk["pending"] = [e["id"] for e in st.get("knowledge_pending", [])]
        seen_on_disk["outbox"] = [e["insight"] for e in st.get("knowledge_outbox", [])]

    with mock.patch(
        "ouroboros.knowledge_base.add_entries", side_effect=OSError("disk full")
    ), mock.patch.object(moltbook, "save_state", side_effect=capture):
        moltbook._record_knowledge(state, entries, processed)

    assert [e["insight"] for e in state["knowledge_outbox"]] == ["i1"]
    assert state["knowledge_pending"] == []
    # The point: what reached disk cannot hold the posts *and* their insights.
    # A crash right after would otherwise extract and record them twice.
    assert seen_on_disk == {"pending": [], "outbox": ["i1"]}


def test_a_post_is_not_released_if_recording_never_ran():
    """The window the two-phase split closes: an exception between extracting
    and recording used to lose both the insight and the post."""
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(2))
    with mock.patch.object(
        llm_mod, "extract_insights_batch",
        return_value=[{"post_index": 0, "insight": "i1", "tags": []}],
    ):
        moltbook._drain_extraction_queue(state, object())
        # _release_extracted is never reached.

    assert [e["id"] for e in state["knowledge_pending"]] == ["p1", "p2"]


def test_the_cap_never_evicts_a_batch_that_is_mid_extraction():
    """_record_knowledge saves on a write failure, and save_state trims -- with
    the batch still queued, because release comes after recording. Evicting it
    there would lose an insight already paid for."""
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(
        state, _posts(moltbook.MAX_KNOWLEDGE_PENDING + 20)
    )

    def trim_midway(_client, batch, *a, **kw):
        # Stand in for the save_state that _record_knowledge performs.
        moltbook._trim_state(state)
        return [{"post_index": 0, "insight": "i", "tags": []}]

    with mock.patch.object(
        llm_mod, "extract_insights_batch", side_effect=trim_midway
    ):
        entries, processed = moltbook._drain_extraction_queue(state, object())

    assert processed == ["p1", "p2", "p3", "p4", "p5"]
    still_queued = {e["id"] for e in state["knowledge_pending"]}
    assert set(processed) <= still_queued, "the in-flight batch was evicted"

    moltbook._release_extracted(state, processed)
    assert not (set(processed) & {e["id"] for e in state["knowledge_pending"]})
    assert len(state["knowledge_pending"]) <= moltbook.MAX_KNOWLEDGE_PENDING


def test_a_failed_batch_stops_being_in_flight():
    """Otherwise a batch that failed would be immune to the cap forever."""
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(3))
    with mock.patch.object(llm_mod, "extract_insights_batch", return_value=None):
        moltbook._drain_extraction_queue(state, object())

    assert not any(e.get("in_flight") for e in state["knowledge_pending"])


def test_release_does_not_purge_entries_whose_id_is_missing():
    """None in the released set would match every id-less entry at once."""
    from ouroboros import moltbook

    state = {"knowledge_pending": [
        {"id": "keep", "title": "", "content": "", "attempts": 0},
        {"title": "no id at all", "content": "", "attempts": 0},
    ]}
    moltbook._release_extracted(state, [None])
    assert len(state["knowledge_pending"]) == 2


def test_a_successful_write_releases_the_batch_in_the_same_step():
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(2))
    with mock.patch.object(
        llm_mod, "extract_insights_batch",
        return_value=[{"post_index": 0, "insight": "i1", "tags": []}],
    ):
        entries, processed = moltbook._drain_extraction_queue(state, object())

    with mock.patch("ouroboros.knowledge_base.add_entries") as add:
        moltbook._record_knowledge(state, entries, processed)

    add.assert_called_once()
    assert state["knowledge_pending"] == []


def test_a_stale_in_flight_mark_does_not_outlive_the_cycle():
    """If a cycle is interrupted between extracting and releasing, the mark is
    left behind. It must not make those entries immune to the cap forever."""
    from ouroboros import llm as llm_mod, moltbook

    state = {"knowledge_pending": [
        {"id": "stale", "title": "", "content": "", "attempts": 0,
         "in_flight": True},
        {"id": "fresh", "title": "", "content": "", "attempts": 0},
    ]}
    with mock.patch.object(llm_mod, "extract_insights_batch", return_value=[]):
        _, processed = moltbook._drain_extraction_queue(state, object())

    assert processed == ["stale", "fresh"]
    moltbook._release_extracted(state, processed)
    assert state["knowledge_pending"] == []


def test_failure_bookkeeping_lands_in_state_and_survives_serialisation():
    """Attempt counts have to reach disk for the give-up rule to mean anything
    across a restart, so they must live in state and round-trip through JSON.
    That run_loop checkpoints them is the caller's half; this is the data's."""
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(state, _posts(2))
    with mock.patch.object(llm_mod, "extract_insights_batch", return_value=None):
        moltbook._drain_extraction_queue(state, object())

    # What a save would write, not just what the object holds.
    persisted = json.loads(json.dumps(state))
    assert [e["attempts"] for e in persisted["knowledge_pending"]] == [1, 1]
    assert not any(e.get("in_flight") for e in persisted["knowledge_pending"])


def test_the_retry_rule_survives_a_saturated_queue():
    """A sustained extraction outage is what saturates the queue, so the cap
    and the retry rule collide exactly when retries matter. The cap used to
    drop the failed batch off the head before its second attempt, making
    MAX_EXTRACTION_ATTEMPTS dead letter under load."""
    from ouroboros import llm as llm_mod, moltbook

    state = {}
    moltbook._queue_for_extraction(
        state, _posts(moltbook.MAX_KNOWLEDGE_PENDING + 10)
    )
    head = ["p1", "p2", "p3", "p4", "p5"]

    with mock.patch.object(llm_mod, "extract_insights_batch", return_value=None):
        for expected in range(1, moltbook.MAX_EXTRACTION_ATTEMPTS):
            moltbook._drain_extraction_queue(state, object())
            moltbook._trim_state(state)
            attempts = {
                e["id"]: e["attempts"]
                for e in state["knowledge_pending"] if e["id"] in head
            }
            assert attempts == dict.fromkeys(head, expected), (
                f"the batch was evicted before attempt {expected + 1}"
            )
            assert len(state["knowledge_pending"]) <= (
                moltbook.MAX_KNOWLEDGE_PENDING
            )

        # The last attempt retires them through the give-up path, not the cap.
        moltbook._drain_extraction_queue(state, object())
        moltbook._trim_state(state)

    remaining = {e["id"] for e in state["knowledge_pending"]}
    assert not (set(head) & remaining)
    assert "p16" in remaining, "the cap shed unprotected entries, as intended"


def test_the_retry_exemption_cannot_grow_beyond_one_batch():
    """Otherwise a sustained outage would exempt the whole queue from the cap
    and it would grow without bound."""
    from ouroboros import moltbook

    state = {"knowledge_pending": [
        {"id": f"p{i}", "title": "", "content": "", "attempts": 1}
        for i in range(moltbook.MAX_KNOWLEDGE_PENDING + 50)
    ]}
    moltbook._trim_state(state)
    assert len(state["knowledge_pending"]) == moltbook.MAX_KNOWLEDGE_PENDING


def test_a_null_title_is_normalised_like_a_null_body():
    """A get default does not fire for an explicit null, so the title reached
    the extraction prompt as the literal "None"."""
    from ouroboros import moltbook

    state = {}
    moltbook._queue_for_extraction(
        state, [{"id": "p1", "title": None, "content": None}]
    )
    assert state["knowledge_pending"][0]["title"] == ""
    assert state["knowledge_pending"][0]["content"] == ""


def test_load_runner_config_reads_the_daily_improvement_cap(tmp_path):
    """The cap lives on SafetyConfig, so the loader has to carry it across.

    Before this was a RunnerConfig field the tracked agent.json could name it
    and nothing read it: the value looked configured and the real ceiling
    stayed at SafetyConfig's default.
    """
    cfg_file = tmp_path / "agent.json"
    cfg_file.write_text(json.dumps({"max_improvements_per_day": 5}))

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

    assert cfg.max_improvements_per_day == 5


def test_the_daily_improvement_cap_defaults_to_the_safety_default():
    """An unset config must not change the ceiling that was already in force."""
    from ouroboros.config import SafetyConfig

    assert RunnerConfig().max_improvements_per_day == SafetyConfig().max_improvements_per_day


# -- get_my_posts with a malformed author (#113) --


@pytest.mark.parametrize("bad", [
    {"id": "1"},                      # author key absent
    {"id": "1", "author": None},      # explicit JSON null
    {"id": "1", "author": "agent"},   # not an object
    {"id": "1", "author": 42},
])
def test_get_my_posts_skips_a_record_with_a_malformed_author(bad):
    """A null author aborted the whole poll instead of skipping one record.

    `.get("author", {})` only falls back when the key is absent, so an
    explicit null raised AttributeError -- and the caller never advanced
    last_comment_check, so every later cycle re-read the same bad feed.
    """
    mine = {"id": "2", "author": {"name": "agent"}}
    with mock.patch("ouroboros.moltbook.get_feed", return_value={"posts": [bad, mine]}):
        assert get_my_posts("key", "agent") == [mine]


def test_load_runner_config_names_the_keys_it_ignores(tmp_path, caplog):
    """An unread key is worse than a rejected one.

    Every setting is pulled out of the file by name, so a typo -- or a cap
    that is a compile-time constant rather than a runner setting -- parses
    cleanly and then does nothing. The operator sees a saved file and keeps
    the old limit.
    """
    with caplog.at_level("WARNING"):
        cfg = _runner_config_from(
            tmp_path,
            {
                "interval_seconds": 60,
                "intervals_seconds": 30,
                "max_lines_changed_per_pr": 500,
            },
        )

    # The keys that are real still apply: one bad key does not stop startup.
    assert cfg.interval_seconds == 60
    assert "intervals_seconds" in caplog.text
    assert "did you mean interval_seconds?" in caplog.text
    assert "max_lines_changed_per_pr" in caplog.text


def test_load_runner_config_is_quiet_about_a_valid_file(tmp_path, caplog):
    """The warning has to stay rare enough to be worth reading."""
    with caplog.at_level("WARNING"):
        cfg = _runner_config_from(
            tmp_path,
            {"interval_seconds": 60, "max_improvements_per_day": 5, "keyword_allowlist": None},
        )

    assert cfg.max_improvements_per_day == 5
    assert caplog.text == ""
