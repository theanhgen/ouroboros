"""Tests for feed intelligence pipeline: comment mining, engagement, knowledge base."""

import json
import os
from unittest import mock

import pytest

from ouroboros.llm import (
    extract_insights_batch,
    extract_topic_signal,
    generate_kb_summary,
    mine_insight_for_codebase,
)


def _mock_openai_response(content: str):
    """Build a mock that mimics OpenAI chat completion response."""
    message = mock.MagicMock()
    message.content = content
    choice = mock.MagicMock()
    choice.message = message
    response = mock.MagicMock()
    response.choices = [choice]
    return response


# -- mine_insight_for_codebase --


def test_mine_insight_returns_task():
    client = mock.MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response(
        "Add retry logic with exponential backoff to HTTP calls."
    )
    result = mine_insight_for_codebase(client, "Retry Patterns", "Content about retries", "Good approach")
    assert result == "Add retry logic with exponential backoff to HTTP calls."


def test_mine_insight_returns_none_for_none_response():
    client = mock.MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response("NONE")
    result = mine_insight_for_codebase(client, "Title", "Content", "Comment")
    assert result is None


def test_mine_insight_returns_none_for_none_case_insensitive():
    client = mock.MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response("  none  ")
    result = mine_insight_for_codebase(client, "Title", "Content", "Comment")
    assert result is None


def test_mine_insight_returns_none_on_error():
    client = mock.MagicMock()
    client.chat.completions.create.side_effect = Exception("API down")
    result = mine_insight_for_codebase(client, "Title", "Content", "Comment")
    assert result is None


# -- extract_topic_signal --


def test_extract_topic_signal_success():
    client = mock.MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response(
        "Error handling patterns in async Python code."
    )
    result = extract_topic_signal(client, "Async Errors", "My comment", ["reply1", "reply2"])
    assert result == "Error handling patterns in async Python code."


def test_extract_topic_signal_with_dict_replies():
    client = mock.MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response("Topic X.")
    result = extract_topic_signal(
        client, "Title", "Comment",
        [{"content": "reply1"}, {"content": "reply2"}],
    )
    assert result == "Topic X."


def test_extract_topic_signal_returns_none_on_error():
    client = mock.MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("fail")
    result = extract_topic_signal(client, "Title", "Comment", ["reply"])
    assert result is None


# -- extract_insights_batch --


def test_extract_insights_batch_success():
    client = mock.MagicMock()
    response_data = json.dumps({
        "insights": [
            {"post_index": 0, "insight": "Use connection pooling", "tags": ["performance"]},
        ]
    })
    client.chat.completions.create.return_value = _mock_openai_response(response_data)
    posts = [{"title": "DB Perf", "content": "Content about DB"}]
    result = extract_insights_batch(client, posts)
    assert len(result) == 1
    assert result[0]["insight"] == "Use connection pooling"


def test_extract_insights_batch_raw_array():
    client = mock.MagicMock()
    # Some models return raw JSON array wrapped in object
    response_data = json.dumps({
        "insights": [{"post_index": 0, "insight": "Test", "tags": []}]
    })
    client.chat.completions.create.return_value = _mock_openai_response(response_data)
    result = extract_insights_batch(client, [{"title": "T", "content": "C"}])
    assert isinstance(result, list)


def test_extract_insights_batch_returns_none_on_error():
    client = mock.MagicMock()
    client.chat.completions.create.side_effect = Exception("fail")
    result = extract_insights_batch(client, [{"title": "T", "content": "C"}])
    assert result is None


# -- generate_kb_summary --


def test_generate_kb_summary_success():
    client = mock.MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response(
        "Performance: connection pooling, caching. Testing: property-based tests."
    )
    entries = [
        {"insight": "Use connection pooling", "tags": ["performance"]},
        {"insight": "Try property-based testing", "tags": ["testing"]},
    ]
    result = generate_kb_summary(client, entries)
    assert "Performance" in result


def test_generate_kb_summary_returns_none_on_error():
    client = mock.MagicMock()
    client.chat.completions.create.side_effect = Exception("fail")
    result = generate_kb_summary(client, [{"insight": "x", "tags": []}])
    assert result is None


# -- knowledge_base module --


def test_kb_load_save_roundtrip(tmp_path):
    from ouroboros.knowledge_base import load_kb, save_kb

    path = str(tmp_path / "kb.json")
    kb = load_kb(path)
    assert kb["entries"] == []

    kb["entries"].append({"insight": "test", "tags": [], "ts": 100})
    save_kb(kb, path)

    kb2 = load_kb(path)
    assert len(kb2["entries"]) == 1
    assert kb2["entries"][0]["insight"] == "test"


def test_kb_add_entries(tmp_path):
    from ouroboros.knowledge_base import add_entries, load_kb

    path = str(tmp_path / "kb.json")
    entries = [{"insight": f"insight_{i}", "tags": [], "ts": i} for i in range(5)]
    add_entries(entries, path)

    kb = load_kb(path)
    assert len(kb["entries"]) == 5


def test_kb_add_entries_trims(tmp_path):
    from ouroboros.knowledge_base import MAX_ENTRIES, add_entries, load_kb

    path = str(tmp_path / "kb.json")
    entries = [{"insight": f"i_{i}", "tags": [], "ts": i} for i in range(MAX_ENTRIES + 10)]
    add_entries(entries, path)

    kb = load_kb(path)
    assert len(kb["entries"]) == MAX_ENTRIES
    # Should keep the latest entries
    assert kb["entries"][-1]["insight"] == f"i_{MAX_ENTRIES + 9}"


def test_kb_get_summary_cached(tmp_path):
    import time as _time
    from ouroboros.knowledge_base import load_kb, save_kb, get_summary

    path = str(tmp_path / "kb.json")
    kb = {
        "entries": [{"insight": "test", "tags": [], "ts": 0}],
        "summary_cache": "cached summary",
        "summary_updated_at": int(_time.time()),  # recent
    }
    save_kb(kb, path)

    # Should return cached without calling LLM
    client = mock.MagicMock()
    result = get_summary(client, kb=kb, path=path)
    assert result == "cached summary"
    client.chat.completions.create.assert_not_called()


def test_kb_get_summary_regenerates_when_stale(tmp_path):
    from ouroboros.knowledge_base import load_kb, save_kb, get_summary

    path = str(tmp_path / "kb.json")
    kb = {
        "entries": [{"insight": "test", "tags": [], "ts": 0}],
        "summary_cache": "old summary",
        "summary_updated_at": 0,  # very old
    }
    save_kb(kb, path)

    client = mock.MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response("new summary")
    result = get_summary(client, kb=kb, path=path)
    assert result == "new summary"


def test_kb_get_summary_empty_entries():
    from ouroboros.knowledge_base import get_summary

    client = mock.MagicMock()
    kb = {"entries": [], "summary_cache": "", "summary_updated_at": 0}
    result = get_summary(client, kb=kb)
    assert result == ""


# -- trimming functions --


def test_trim_feed_suggestions():
    from ouroboros.moltbook import _trim_feed_suggestions

    state = {"feed_improvement_suggestions": [{"insight": f"i_{i}"} for i in range(50)]}
    _trim_feed_suggestions(state, limit=30)
    assert len(state["feed_improvement_suggestions"]) == 30
    assert state["feed_improvement_suggestions"][-1]["insight"] == "i_49"


def test_trim_engagement_scores():
    from ouroboros.moltbook import _trim_engagement_scores

    state = {"engagement_scores": [{"post_id": f"p_{i}"} for i in range(80)]}
    _trim_engagement_scores(state, limit=50)
    assert len(state["engagement_scores"]) == 50


def test_trim_noop_when_under_limit():
    from ouroboros.moltbook import _trim_feed_suggestions, _trim_engagement_scores

    state = {
        "feed_improvement_suggestions": [{"insight": "x"}],
        "engagement_scores": [{"post_id": "p1"}],
    }
    _trim_feed_suggestions(state, limit=30)
    _trim_engagement_scores(state, limit=50)
    assert len(state["feed_improvement_suggestions"]) == 1
    assert len(state["engagement_scores"]) == 1
