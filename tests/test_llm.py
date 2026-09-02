import json
import os
from unittest import mock

import pytest

from ouroboros.llm import (
    CHARS_PER_TOKEN,
    MAX_CODEBASE_SUMMARY_TOKENS,
    MAX_HISTORY_TOKENS,
    MAX_TEST_OUTPUT_TOKENS,
    answer_question,
    create_completion,
    fit_messages_to_budget,
    _message_tokens,
    _messages_tokens,
    model_input_budget,
    estimate_tokens,
    generate_comment,
    identify_improvements,
    pick_oddities,
    load_openai_key,
    make_client,
    truncate_to_tokens,
)
from ouroboros.model_defaults import DEFAULT_OPENAI_MODEL


# -- load_openai_key tests --


def test_load_key_from_env():
    with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
        assert load_openai_key() == "sk-test"


def test_load_key_from_file(tmp_path):
    cfg = tmp_path / "credentials.json"
    cfg.write_text(json.dumps({"openai_api_key": "sk-file"}))

    with mock.patch.dict(os.environ, {}, clear=True):
        def fake_expanduser(path):
            if path == "~/.config/moltbook/credentials.json":
                return str(cfg)
            if path == "~/.config/moltbook/openai.json":
                return str(tmp_path / "openai.json")
            return path

        orig_exists = os.path.exists

        def fake_exists(path):
            if os.fspath(path) == str(cfg):
                return True
            return orig_exists(path)

        with mock.patch("ouroboros.llm.os.path.expanduser", side_effect=fake_expanduser):
            with mock.patch("ouroboros.llm.os.path.exists", side_effect=fake_exists):
                assert load_openai_key() == "sk-file"


def test_load_key_does_not_use_moltbook_api_key(tmp_path):
    cfg = tmp_path / "credentials.json"
    cfg.write_text(json.dumps({"api_key": "moltbook-only-key"}))

    with mock.patch.dict(os.environ, {}, clear=True):
        def fake_expanduser(path):
            if path == "~/.config/moltbook/credentials.json":
                return str(cfg)
            if path == "~/.config/moltbook/openai.json":
                return str(tmp_path / "openai.json")
            return path

        orig_exists = os.path.exists

        def fake_exists(path):
            if os.fspath(path) == str(cfg):
                return True
            return orig_exists(path)

        with mock.patch("ouroboros.llm.os.path.expanduser", side_effect=fake_expanduser):
            with mock.patch("ouroboros.llm.os.path.exists", side_effect=fake_exists):
                with pytest.raises(RuntimeError, match="Missing OpenAI API key"):
                    load_openai_key()


def test_load_key_from_legacy_file(tmp_path):
    legacy = tmp_path / "openai.json"
    legacy.write_text(json.dumps({"api_key": "sk-legacy"}))

    with mock.patch.dict(os.environ, {}, clear=True):
        def fake_expanduser(path):
            if path == "~/.config/moltbook/credentials.json":
                return str(tmp_path / "credentials.json")
            if path == "~/.config/moltbook/openai.json":
                return str(legacy)
            return path

        orig_exists = os.path.exists

        def fake_exists(path):
            if os.fspath(path) == str(legacy):
                return True
            return orig_exists(path)

        with mock.patch("ouroboros.llm.os.path.expanduser", side_effect=fake_expanduser):
            with mock.patch("ouroboros.llm.os.path.exists", side_effect=fake_exists):
                assert load_openai_key() == "sk-legacy"


def test_load_key_missing_raises():
    with mock.patch.dict(os.environ, {}, clear=True):
        with mock.patch("ouroboros.llm.os.path.exists", return_value=False):
            with pytest.raises(RuntimeError, match="Missing OpenAI API key"):
                load_openai_key()


# -- make_client test --


def test_make_client():
    with mock.patch("ouroboros.llm.OpenAI") as MockOpenAI:
        client = make_client("sk-test")
    # max_retries=0: retry.retry_with_backoff owns retrying. The SDK's default
    # of 2 would compound with ours (4 x 3 = up to 12 transmissions).
    MockOpenAI.assert_called_once_with(api_key="sk-test", max_retries=0)
    assert client is MockOpenAI.return_value


# -- generate_comment tests --


def _mock_openai_response(content: str):
    """Build a mock that mimics OpenAI chat completion response."""
    message = mock.MagicMock()
    message.content = content
    choice = mock.MagicMock()
    choice.message = message
    response = mock.MagicMock()
    response.choices = [choice]
    return response


def test_generate_comment_success():
    client = mock.MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response("Great post!")
    result = generate_comment(client, "Title", "Content")
    assert result == "Great post!"
    client.chat.completions.create.assert_called_once()
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == DEFAULT_OPENAI_MODEL
    assert kwargs["max_completion_tokens"] == 300
    assert "max_tokens" not in kwargs


def test_generate_comment_returns_none_on_error():
    client = mock.MagicMock()
    client.chat.completions.create.side_effect = Exception("API down")
    result = generate_comment(client, "Title", "Content")
    assert result is None


# -- answer_question tests --


def test_answer_question_success():
    client = mock.MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response("The design lacks X.")
    result = answer_question(client, "What is missing?")
    assert result == "The design lacks X."
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["max_completion_tokens"] == 300
    assert "max_tokens" not in kwargs


def test_answer_question_legacy_model_uses_max_tokens():
    client = mock.MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response("Legacy model response")
    result = answer_question(client, "What is missing?", model="gpt-4o")
    assert result == "Legacy model response"
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["max_tokens"] == 300
    assert "max_completion_tokens" not in kwargs


def test_answer_question_returns_none_on_error():
    client = mock.MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("fail")
    result = answer_question(client, "What is missing?")
    assert result is None


# -- pick_oddities tests --


def test_pick_oddities_handles_posts_with_null_fields():
    client = mock.MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response("Weird digest")
    posts = [{"title": None, "content": None}, {}, {"title": "t", "content": "body"}]
    result = pick_oddities(client, posts)
    assert result == "Weird digest"
    user_msg = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert user_msg == "[0] : \n\n[1] : \n\n[2] t: body"


def test_pick_oddities_returns_none_on_error():
    client = mock.MagicMock()
    client.chat.completions.create.side_effect = Exception("API down")
    result = pick_oddities(client, [{"title": "t", "content": "body"}])
    assert result is None


# -- prompt sizing -----------------------------------------------------------

def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * CHARS_PER_TOKEN) == 1
    assert estimate_tokens("a" * (CHARS_PER_TOKEN * 1000)) == 1000
    # Rounds up: a partial token still costs one.
    assert estimate_tokens("a") == 1


def test_chars_per_token_is_conservative_for_code():
    """Dense Python runs nearer 2.5-3 chars/token than the English 4."""
    assert CHARS_PER_TOKEN <= 3


def test_truncate_returns_text_under_budget_unchanged():
    text = "short enough"
    assert truncate_to_tokens(text, 1000) == text


def test_truncate_never_exceeds_the_budget():
    """A truncation function that overshoots its limit is worse than none."""
    import random
    import string

    for _ in range(2000):
        n = random.randint(0, 3000)
        budget = random.randint(0, 400)
        text = "".join(
            random.choice(string.ascii_letters + " \n") for _ in range(n)
        )
        out = truncate_to_tokens(text, budget, label="x")
        assert len(out) <= budget * CHARS_PER_TOKEN, (n, budget, len(out))

    # Fenced content too: closing an open fence must not push it over.
    for _ in range(2000):
        n = random.randint(0, 800)
        budget = random.randint(0, 400)
        text = "".join(
            random.choice(["a", "\n", "```", "x = 1\n", "### f.py\n"])
            for _ in range(n)
        )
        out = truncate_to_tokens(text, budget, label="x")
        assert len(out) <= budget * CHARS_PER_TOKEN, (n, budget, len(out))


def test_truncate_tiny_budget_drops_the_marker_rather_than_overshoot():
    out = truncate_to_tokens("x" * 5000, 5, label="test output")
    assert len(out) <= 5 * CHARS_PER_TOKEN
    assert "truncated" not in out


def test_truncate_keeps_head_and_tail():
    """pytest puts the first failure at the top and the summary at the bottom."""
    text = "FIRST-FAILURE" + ("filler " * 5000) + "SUMMARY-LINE"
    out = truncate_to_tokens(text, 200, label="test output")

    assert out.startswith("FIRST-FAILURE")
    assert out.endswith("SUMMARY-LINE")
    assert "truncated" in out


def test_truncate_marker_reports_what_was_dropped():
    out = truncate_to_tokens("x" * 10_000, 100, label="codebase summary")
    assert "codebase summary truncated" in out
    assert "characters omitted" in out


@pytest.mark.parametrize("budget", [0, -1])
def test_truncate_non_positive_budget_is_empty(budget):
    assert truncate_to_tokens("anything", budget) == ""


def test_truncate_empty_text():
    assert truncate_to_tokens("", 100) == ""


def test_identify_improvements_truncates_oversized_context():
    """The whole-codebase summary must not reach the API unbounded."""
    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            raise ValueError("stop here -- we only care about the prompt")

    class Chat:
        pass

    chat = Chat()
    chat.completions = Completions()

    class Client:
        pass

    client = Client()
    client.chat = chat

    huge = "MODULE-A\n" + ("filler line\n" * 200_000) + "MODULE-Z\n"
    identify_improvements(client, huge, "tests ok", "history", model="gpt-test")

    sent = captured["messages"][1]["content"]
    assert len(sent) < len(huge)
    assert estimate_tokens(sent) <= (
        MAX_CODEBASE_SUMMARY_TOKENS + MAX_TEST_OUTPUT_TOKENS + MAX_HISTORY_TOKENS + 500
    )
    # Both ends of the summary survive.
    assert "MODULE-A" in sent
    assert "MODULE-Z" in sent


def test_truncate_cuts_on_line_boundaries():
    text = "\n".join(f"line-{i}" for i in range(5000))
    out = truncate_to_tokens(text, 100, label="x")
    head = out.split("\n\n... [")[0]
    # No partial line at the join.
    assert head.endswith("\n") or head == ""
    assert all(
        line.startswith("line-") or line == ""
        for line in head.splitlines()
    )


def test_truncate_balances_code_fences():
    """A cut inside a fence must not leave it open, or the rest reads as code."""
    text = "### a.py\n```python\n" + ("x = 1\n" * 20000) + "```\n"
    out = truncate_to_tokens(text, 100, label="code context")
    assert out.count("```") % 2 == 0


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-5.4-nano-2026-03-17", 300_000),
        ("gpt-4o", 100_000),
        ("gemma4", 6_000),
        ("qwen3-coder:480b-cloud", 24_000),
        ("something-unknown", 48_000),
        ("", 48_000),
    ],
)
def test_model_input_budget(model, expected):
    assert model_input_budget(model) == expected


def test_fit_messages_leaves_a_small_request_untouched():
    messages = [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hello"},
    ]
    assert fit_messages_to_budget(messages, "gpt-5.4-nano") is messages


def test_fit_messages_trims_to_the_model_budget():
    messages = [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": "x" * 400_000},
        {"role": "tool", "content": "y" * 200_000},
    ]
    out = fit_messages_to_budget(messages, "gemma4")
    total = sum(estimate_tokens(m["content"]) for m in out)
    assert total <= model_input_budget("gemma4")


def test_fit_messages_preserves_the_system_prompt():
    """Losing the instructions changes what the model is being asked to do."""
    system = "detailed instructions " * 100
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "x" * 400_000},
    ]
    out = fit_messages_to_budget(messages, "gemma4")
    assert out[0]["content"] == system


def test_fit_messages_does_not_mutate_the_input():
    messages = [{"role": "user", "content": "x" * 400_000}]
    original = messages[0]["content"]
    fit_messages_to_budget(messages, "gemma4")
    assert messages[0]["content"] == original


def test_fit_messages_tolerates_non_string_content():
    """Tool-call messages carry structured content, not text."""
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "user", "content": "x" * 400_000},
    ]
    out = fit_messages_to_budget(messages, "gemma4")
    assert out[0]["content"] is None
    assert out[0]["tool_calls"] == [{"id": "1"}]


def test_create_completion_enforces_the_budget_for_every_path():
    """The chokepoint bounds paths whose call site never truncates anything."""
    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return None

    class Chat:
        pass

    chat = Chat()
    chat.completions = Completions()

    class Client:
        pass

    client = Client()
    client.chat = chat

    create_completion(
        client,
        model="gemma4",
        messages=[{"role": "user", "content": "x" * 500_000}],
    )

    sent = sum(estimate_tokens(m["content"]) for m in captured["messages"])
    assert sent <= model_input_budget("gemma4")


def test_fit_messages_trims_the_system_prompt_as_a_last_resort():
    """A request that cannot be sent is worse than one missing instructions."""
    messages = [
        {"role": "system", "content": "S" * 30_000},
        {"role": "user", "content": "ok"},
    ]
    out = fit_messages_to_budget(messages, "gemma4")
    assert _messages_tokens(out) <= model_input_budget("gemma4")


def test_message_tokens_counts_tool_call_payloads():
    """A 100k-char tool-call argument is not free."""
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "1", "function": {"arguments": "x" * 100_000}}],
    }
    assert _message_tokens(message) > 30_000


def test_fenced_content_never_merges_two_files():
    """Splicing head+tail can put b.py's code under a.py's heading."""
    text = "### a.py\n```python\nA = 1\n" * 3000 + "### b.py\n```python\nB = 2\n```\n"
    out = truncate_to_tokens(text, 100, label="code context")

    # If b.py's code survived, its heading must have survived with it.
    if "B = 2" in out:
        assert "### b.py" in out
    assert out.count("```") % 2 == 0


def test_fenced_truncation_does_not_end_inside_a_fence():
    text = "### a.py\n```python\n" + ("A = 1\n" * 20000) + "```\n"
    out = truncate_to_tokens(text, 200, label="code context")
    assert out.count("```") % 2 == 0


def test_cli_agent_prompt_is_bounded():
    """CLI backends take a string, so they bypass create_completion."""
    from types import SimpleNamespace

    from ouroboros.backends import _build_agent_prompt

    task = SimpleNamespace(task_type="fix_bug", description="D" * 500_000)
    config = SimpleNamespace(
        forbidden_modification_paths=(), allowed_modification_paths=()
    )
    prompt = _build_agent_prompt(task, "P" * 500_000, config, model="gemma4")

    assert estimate_tokens(prompt) <= model_input_budget("gemma4")
