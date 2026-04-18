import json
import os
from unittest import mock

import pytest

from ouroboros.llm import answer_question, generate_comment, load_openai_key, make_client
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

        def fake_exists(path):
            return path == str(cfg)

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

        def fake_exists(path):
            return path == str(cfg)

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

        def fake_exists(path):
            return path == str(legacy)

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
    MockOpenAI.assert_called_once_with(api_key="sk-test")
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
