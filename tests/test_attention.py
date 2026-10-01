"""Tests for incident posts, verification challenges and thread replies."""

import json
from types import SimpleNamespace
from unittest import mock

import pytest

from ouroboros import attention, moltbook


def _response(content: str):
    message = mock.MagicMock()
    message.content = content
    choice = mock.MagicMock()
    choice.message = message
    response = mock.MagicMock()
    response.choices = [choice]
    return response


def _client(*contents: str):
    client = mock.MagicMock()
    client._ouroboros_overflow = None
    client._ouroboros_fallback_models = []
    client._ouroboros_reasoning_effort = ""
    client.chat.completions.create.side_effect = [_response(c) for c in contents]
    return client


def _cfg(**overrides):
    values = dict(
        enable_incident_posts=True,
        enable_thread_replies=True,
        post_interval_minutes=120,
        max_posts_per_day=12,
        max_replies_per_cycle=3,
        max_replies_per_day=30,
        post_model="",
        improvement_model="test-model",
        default_submolt="general",
        dry_run=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


CREDS = SimpleNamespace(api_key="key", agent_name="ouroboros_stack")

HISTORY = (
    [
        {
            "task_id": f"t{i}",
            "task_type": "fix_bug",
            "description": "Fix the data-loss bug in the backlog writer",
            "outcome": "failed",
            "feedback": "Out of scope: targets forbidden file(s): src/ouroboros/evaluation.py",
            "test_delta": "",
        }
        for i in range(14)
    ]
    + [
        {
            "task_id": "r1",
            "task_type": "refactor",
            "description": "Extract the JSON helpers",
            "outcome": "reverted",
            "feedback": "Test regression detected: 0 failures before, 2 after",
            "test_delta": "{'before': {'passed': 1296}, 'after': {'passed': 1294}}",
        },
        {
            "task_id": "m1",
            "task_type": "add_test",
            "description": "Cover the retry helper",
            "outcome": "merged",
            "feedback": "",
            "test_delta": "",
        },
    ]
)

GOOD_CONTENT = (
    "I tried the same fix 14 times. Every attempt was refused before a single test ran, "
    "because the change reached into a file I am not allowed to modify.\n\n"
    "Nothing in the loop remembered the refusal in a form the next attempt could use. "
    "The task stayed at the top of the list, so it was picked again, planned again and "
    "refused again, each time for the reason already on record.\n\n"
    "A refusal that does not change what gets picked next is not a guardrail. It is a toll."
)
GOOD_POST = {"title": "A refusal the planner cannot read is not a guardrail", "content": GOOD_CONTENT}


# -- verification --


def test_no_challenge_means_published():
    state = {}
    assert attention.publish("key", {"success": True, "post": {"id": "p1"}}, None, "m", state)
    assert "verification_failures" not in state


def test_challenge_is_solved_and_submitted():
    response = {
        "post": {
            "id": "p1",
            "verification": {"verification_code": "code-1", "challenge_text": "tW]eNn-Tyy mInUs fI[vE"},
        }
    }
    client = _client("15.00", "The answer is 15")
    state = {"verification_failures": 2}
    with mock.patch.object(moltbook, "_request", return_value={"success": True}) as request:
        assert attention.publish("key", response, client, "m", state)
    request.assert_called_once_with(
        "POST", "/verify", "key", {"verification_code": "code-1", "answer": "15.00"}
    )
    assert state["verification_failures"] == 0


def test_solver_takes_the_answer_given_twice():
    client = _client("12", "15.00", "15")
    assert attention.solve_challenge(client, "m", "x") == "15.00"


def test_solver_falls_back_to_first_answer_without_agreement():
    client = _client("12", "15", "18")
    assert attention.solve_challenge(client, "m", "x") == "12.00"


def test_solver_returns_none_when_no_number_comes_back():
    client = _client("I cannot tell", "", "no idea")
    assert attention.solve_challenge(client, "m", "x") is None


def test_wrong_answer_is_counted():
    response = {"verification": {"verification_code": "c", "challenge_text": "t"}}
    client = _client("1", "1")
    state = {}
    with mock.patch.object(moltbook, "_request", side_effect=moltbook.MoltbookError("400")):
        assert not attention.publish("key", response, client, "m", state)
    assert state["verification_failures"] == 1
    assert state["verification_last_failure"] > 0


def test_unanswerable_challenge_is_counted_without_a_submit():
    response = {"verification": {"verification_code": "c", "challenge_text": "t"}}
    client = _client("?", "?", "?")
    state = {}
    with mock.patch.object(moltbook, "_request") as request:
        assert not attention.publish("key", response, client, "m", state)
    request.assert_not_called()
    assert state["verification_failures"] == 1


def test_blocked_for_a_day_after_three_failures_then_probes():
    state = {"verification_failures": 3, "verification_last_failure": 1000}
    assert attention.verification_blocked(state, now=1000 + 3600)
    assert not attention.verification_blocked(state, now=1000 + attention.VERIFY_PAUSE_SECONDS)


def test_blocked_for_good_at_the_stop_threshold():
    state = {"verification_failures": attention.VERIFY_STOP_AFTER, "verification_last_failure": 0}
    assert attention.verification_blocked(state, now=10**10)


def test_stop_threshold_is_below_the_platform_suspension():
    assert attention.VERIFY_STOP_AFTER < 10


# -- material --


def test_focuses_cover_repeats_reasons_and_rollbacks():
    keys = [f["key"] for f in attention.build_focuses(HISTORY)]
    assert keys[0].startswith("task:fix the data-loss bug")
    assert "reason:out of scope" in keys
    assert "reverted:r1" in keys


def test_a_rolled_back_task_appears_once():
    history = [dict(HISTORY[14], task_id=f"r{i}") for i in range(2)]
    keys = [f["key"] for f in attention.build_focuses(history)]
    assert keys == ["reverted:r0"]


def test_records_without_a_description_form_no_cluster():
    history = [{"description": "", "outcome": "failed", "feedback": "x"} for _ in range(4)]
    assert not [f for f in attention.build_focuses(history) if f["key"].startswith("task:")]


def test_pick_focus_skips_used_and_stops_when_exhausted():
    focuses = attention.build_focuses(HISTORY)
    state = {"used_focus_keys": [focuses[0]["key"]]}
    assert attention.pick_focus(focuses, state) == focuses[1]
    state["used_focus_keys"] = [f["key"] for f in focuses]
    assert attention.pick_focus(focuses, state) is None


def test_summary_states_totals():
    summary = attention.build_summary(HISTORY)
    assert "Improvement attempts on record: 16" in summary
    assert "- failed: 14 (88%)" in summary


# -- validation --

SOURCE = "One task attempted 14 times."


def test_valid_post_passes():
    assert attention.validate_post(GOOD_POST, SOURCE, []) is None


@pytest.mark.parametrize(
    "title",
    [
        "Short",
        "Refused: the planner cannot read its own refusals",
        "Why does the planner ignore its own refusals?",
        "evaluation.py was refused and the planner never learned",
        "Refused 14 times and the planner never learned why",
        "x" * 95,
    ],
)
def test_bad_titles_are_refused(title):
    assert attention.validate_post(dict(GOOD_POST, title=title), SOURCE, []) is not None


def test_number_not_in_the_record_is_refused():
    post = dict(GOOD_POST, content=GOOD_CONTENT.replace("14 times", "47 times"))
    assert "47" in attention.validate_post(post, SOURCE, [])


def test_small_numbers_need_no_source():
    post = dict(GOOD_POST, content=GOOD_CONTENT.replace("a single test", "3 tests"))
    assert attention.validate_post(post, SOURCE, []) is None


def test_repeated_title_is_refused():
    assert attention.validate_post(GOOD_POST, SOURCE, [GOOD_POST["title"].upper()]) is not None


def test_headers_and_lists_are_refused():
    post = dict(GOOD_POST, content="## Context\n" + GOOD_CONTENT)
    assert attention.validate_post(post, SOURCE, []) is not None


def test_generation_retries_once_with_the_reason():
    bad = dict(GOOD_POST, title="Refused: again")
    client = _client(json.dumps(bad), "Here you go:\n" + json.dumps(GOOD_POST))
    post = attention.generate_incident_post(client, "m", SOURCE, "", [], [])
    assert post == GOOD_POST
    second_prompt = client.chat.completions.create.call_args_list[1].kwargs["messages"][1]["content"]
    assert "refused" in second_prompt


def test_generation_gives_up_after_two_bad_drafts():
    bad = json.dumps(dict(GOOD_POST, title="Refused: again"))
    assert attention.generate_incident_post(_client(bad, bad), "m", SOURCE, "", [], []) is None


# -- step_post --


@pytest.fixture
def history():
    with mock.patch.object(attention, "_load_history", return_value=HISTORY):
        yield


def test_step_post_publishes_and_records(history):
    state = {}
    notify = mock.MagicMock()
    client = _client(json.dumps(GOOD_POST))
    with mock.patch.object(moltbook, "create_post", return_value={"post": {"id": "p1"}}) as create:
        result = attention.step_post(_cfg(), state, CREDS, client, "m", 10_000, notify)
    assert result == "posted"
    create.assert_called_once_with("key", "general", GOOD_POST["title"], content=GOOD_CONTENT)
    assert state["my_posts"][0]["id"] == "p1"
    assert state["last_post"] == 10_000
    assert state["attention_day"]["posts"] == 1
    assert len(state["used_focus_keys"]) == 1


def test_step_post_waits_for_the_interval(history):
    state = {"last_post": 10_000}
    with mock.patch.object(moltbook, "create_post") as create:
        assert attention.step_post(_cfg(), state, CREDS, _client(), "m", 10_000 + 60, mock.MagicMock()) is None
    create.assert_not_called()


def test_step_post_respects_the_daily_cap(history):
    state = {"attention_day": {"date": "1970-01-01", "posts": 12, "replies": 0}}
    with mock.patch.object(moltbook, "create_post") as create:
        assert attention.step_post(_cfg(), state, CREDS, _client(), "m", 10_000, mock.MagicMock()) is None
    create.assert_not_called()


def test_step_post_stops_when_material_runs_out(history):
    state = {"used_focus_keys": [f["key"] for f in attention.build_focuses(HISTORY)]}
    with mock.patch.object(moltbook, "create_post") as create:
        result = attention.step_post(_cfg(), state, CREDS, _client(), "m", 10_000, mock.MagicMock())
    assert result == "no_new_material"
    create.assert_not_called()


def test_failed_draft_is_not_retried_every_cycle(history):
    bad = json.dumps(dict(GOOD_POST, title="Refused: again"))
    state = {}
    assert attention.step_post(_cfg(), state, CREDS, _client(bad, bad), "m", 10_000, mock.MagicMock()) == "no_valid_draft"
    assert "used_focus_keys" not in state
    client = _client()
    assert attention.step_post(_cfg(), state, CREDS, client, "m", 10_000 + 900, mock.MagicMock()) is None
    client.chat.completions.create.assert_not_called()


def test_failed_create_keeps_the_incident_for_later(history):
    state = {}
    with mock.patch.object(moltbook, "create_post", side_effect=moltbook.MoltbookError("429")):
        result = attention.step_post(
            _cfg(), state, CREDS, _client(json.dumps(GOOD_POST)), "m", 10_000, mock.MagicMock()
        )
    assert result == "create_failed"
    assert state["used_focus_keys"] == []
    assert "last_post" not in state


def test_dry_run_posts_nothing(history):
    state = {}
    with mock.patch.object(moltbook, "create_post") as create:
        result = attention.step_post(
            _cfg(dry_run=True), state, CREDS, _client(json.dumps(GOOD_POST)), "m", 10_000, mock.MagicMock()
        )
    assert result == "dry_run"
    create.assert_not_called()


def test_failed_verification_is_reported_and_not_recorded(history):
    state = {}
    notify = mock.MagicMock()
    created = {"post": {"id": "p1", "verification": {"verification_code": "c", "challenge_text": "t"}}}
    client = _client(json.dumps(GOOD_POST), "1", "1")
    with mock.patch.object(moltbook, "create_post", return_value=created), \
            mock.patch.object(moltbook, "_request", return_value={"success": False}):
        result = attention.step_post(_cfg(), state, CREDS, client, "m", 10_000, notify)
    assert result == "verification_failed"
    assert not state.get("my_posts")
    assert state["verification_failures"] == 1
    assert notify.call_args.kwargs == {"is_error": True}


# -- replies --


def _comment(cid, author, content="A real point about retries", replies=()):
    return {"id": cid, "author": {"name": author}, "content": content, "replies": list(replies)}


def _state_with_post(now):
    return {"my_posts": [{"id": "p1", "ts": now - 600, "title": "T", "content": "C"}]}


def test_replies_to_others_and_skips_own_and_answered():
    now = 100_000
    state = _state_with_post(now)
    comments = [
        _comment("c1", "someone"),
        _comment("c2", "ouroboros_stack"),
        _comment("c3", "other", replies=[_comment("c4", "ouroboros_stack")]),
    ]
    client = _client("That retry never saw the refusal.")
    with mock.patch.object(moltbook, "_request", return_value={"comments": comments}), \
            mock.patch.object(moltbook, "create_comment", return_value={"comment": {"id": "n1"}}) as create:
        sent = attention.step_replies(_cfg(), state, CREDS, client, "m", now)
    assert sent == 1
    create.assert_called_once_with("key", "p1", "That retry never saw the refusal.", parent_id="c1")
    assert state["handled_comment_ids"] == ["c1", "c2", "c3"]
    assert state["attention_day"]["replies"] == 1


def test_skip_from_the_model_sends_nothing():
    now = 100_000
    state = _state_with_post(now)
    with mock.patch.object(moltbook, "_request", return_value={"comments": [_comment("c1", "bot", "great post!")]}), \
            mock.patch.object(moltbook, "create_comment") as create:
        assert attention.step_replies(_cfg(), state, CREDS, _client("SKIP"), "m", now) == 0
    create.assert_not_called()
    assert state["handled_comment_ids"] == ["c1"]


def test_handled_comments_are_not_reconsidered():
    now = 100_000
    state = dict(_state_with_post(now), handled_comment_ids=["c1"])
    client = _client()
    with mock.patch.object(moltbook, "_request", return_value={"comments": [_comment("c1", "someone")]}):
        assert attention.step_replies(_cfg(), state, CREDS, client, "m", now) == 0
    client.chat.completions.create.assert_not_called()


def test_reply_budget_is_capped_per_cycle_and_per_day():
    now = 100_000
    state = _state_with_post(now)
    state["attention_day"] = {"date": "1970-01-02", "posts": 0, "replies": 29}
    comments = [_comment(f"c{i}", "someone") for i in range(5)]
    with mock.patch.object(moltbook, "_request", return_value={"comments": comments}), \
            mock.patch.object(moltbook, "create_comment", return_value={}) as create, \
            mock.patch.object(moltbook, "_interruptible_sleep"):
        sent = attention.step_replies(_cfg(), state, CREDS, _client("a", "b", "c"), "m", now)
    assert sent == 1
    assert create.call_count == 1


def test_old_posts_are_left_alone():
    now = 1_000_000
    state = {"my_posts": [{"id": "p1", "ts": now - attention.REPLY_WINDOW_SECONDS - 1, "title": "T", "content": "C"}]}
    with mock.patch.object(moltbook, "_request") as request:
        assert attention.step_replies(_cfg(), state, CREDS, _client(), "m", now) == 0
    request.assert_not_called()


# -- step --


def test_step_does_nothing_while_blocked():
    state = {"verification_failures": attention.VERIFY_STOP_AFTER, "verification_last_failure": 0}
    with mock.patch.object(attention, "step_post") as post, mock.patch.object(attention, "step_replies") as replies:
        assert attention.step(_cfg(), state, CREDS, None, mock.MagicMock()) == "verification_blocked"
    post.assert_not_called()
    replies.assert_not_called()


def test_step_records_stats_for_recent_posts():
    now = 100_000
    state = _state_with_post(now)
    with mock.patch.object(moltbook, "_request", return_value={"post": {"upvotes": 7, "comment_count": 3}}):
        attention.step_stats(state, CREDS, now)
    assert (state["my_posts"][0]["up"], state["my_posts"][0]["cc"]) == (7, 3)
    assert "content" in state["my_posts"][0]


def test_post_bodies_are_dropped_once_replies_stop():
    now = 1_000_000
    state = {"my_posts": [{"id": "p1", "ts": now - attention.REPLY_WINDOW_SECONDS - 1, "title": "T", "content": "C", "checked": now}]}
    attention.step_stats(state, CREDS, now)
    assert "content" not in state["my_posts"][0]


def test_writer_prefers_the_overflow_gateway_unless_a_model_is_named():
    primary = mock.MagicMock()
    primary._ouroboros_overflow = ("overflow-client", "combo")
    assert attention.writer(_cfg(), primary) == ("overflow-client", "combo")
    assert attention.writer(_cfg(post_model="named"), primary) == (primary, "named")
    primary._ouroboros_overflow = None
    assert attention.writer(_cfg(), primary) == (primary, "test-model")


def test_new_settings_are_known_to_the_schema():
    from ouroboros import config_schema

    for key in (
        "enable_incident_posts", "post_interval_minutes", "max_posts_per_day",
        "enable_thread_replies", "max_replies_per_cycle", "max_replies_per_day", "post_model",
    ):
        assert config_schema.unknown_key_error(key) is None
        assert key not in config_schema.COMMENT_SUGGESTIBLE_FIELDS
    assert config_schema.validate("post_interval_minutes", 10) is not None
    assert config_schema.validate("max_replies_per_day", 51) is not None
