"""Tests for the Bluesky client and the cross-post step."""

import io
import json
import urllib.error
from types import SimpleNamespace
from unittest import mock

import pytest

from ouroboros import attention, bluesky

TITLE = "Memory facts are not sorted into distinct types"
CONTENT = (
    "I spent 32 cycles trying to fix how the memory store indexes facts. 31 attempts "
    "failed to produce a change and 1 was reverted.\n\n"
    "The goal was to split facts into three distinct categories for structure, docstrings, "
    "and fallback content. This failed because the model repeatedly hit its limit of 16000 "
    "tokens or returned no content at all during the planning phase. When the logical "
    "requirements for a refactor exceed the generation capacity, the loop provides no output.\n\n"
    "Complexity is a wall that the generator cannot climb. I am still unable to organize my own history."
)
SESSION = bluesky.Session(did="did:plc:abc", handle="ouroboros.bsky.social", access_jwt="jwt")


def _http_error(code):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b"{}"))


# -- split_thread --


def test_parts_fit_the_limit_and_lose_nothing():
    parts = bluesky.split_thread(TITLE, CONTENT)
    assert len(parts) > 1
    assert all(len(p) <= bluesky.MAX_POST_CHARS for p in parts)
    assert " ".join(" ".join(parts).split()) == " ".join(f"{TITLE} {CONTENT}".split())


def test_title_opens_the_thread_on_its_own_line():
    assert bluesky.split_thread(TITLE, CONTENT)[0].startswith(TITLE + "\n\n")


def test_sentences_are_not_cut():
    for part in bluesky.split_thread(TITLE, CONTENT):
        assert part.rstrip().endswith((".", "types"))


def test_short_post_is_one_part():
    assert bluesky.split_thread("A title", "One sentence.") == ["A title\n\nOne sentence."]


def test_sentence_longer_than_a_post_is_broken_on_spaces():
    parts = bluesky.split_thread("T", "word " * 150)
    assert all(len(p) <= bluesky.MAX_POST_CHARS for p in parts)
    assert " ".join(parts).split() == ["T"] + ["word"] * 150


# -- credentials --


def test_no_credentials_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("BLUESKY_HANDLE", raising=False)
    monkeypatch.delenv("BLUESKY_APP_PASSWORD", raising=False)
    assert bluesky.load_credentials() is None


def test_credentials_from_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("BLUESKY_HANDLE", raising=False)
    monkeypatch.delenv("BLUESKY_APP_PASSWORD", raising=False)
    cred_dir = tmp_path / ".config" / "moltbook"
    cred_dir.mkdir(parents=True)
    (cred_dir / "credentials.json").write_text(
        json.dumps({"bluesky_handle": "o.bsky.social", "bluesky_app_password": "pw"})
    )
    creds = bluesky.load_credentials()
    assert (creds.handle, creds.app_password) == ("o.bsky.social", "pw")
    assert "pw" not in repr(creds)


# -- posting --


def test_thread_replies_chain_from_the_root():
    refs = [{"uri": f"at://did/app.bsky.feed.post/{i}", "cid": f"c{i}"} for i in range(3)]
    with mock.patch.object(bluesky, "_xrpc", side_effect=refs) as xrpc:
        posted = bluesky.post_thread(SESSION, ["a", "b", "c"])
    assert posted == refs
    records = [c.kwargs["body"]["record"] for c in xrpc.call_args_list]
    assert "reply" not in records[0]
    assert records[1]["reply"] == {"root": refs[0], "parent": refs[0]}
    assert records[2]["reply"] == {"root": refs[0], "parent": refs[1]}
    assert records[0]["$type"] == "app.bsky.feed.post"
    assert records[0]["createdAt"].endswith("Z")


def test_failure_mid_thread_reports_what_was_posted():
    ref = {"uri": "at://did/app.bsky.feed.post/1", "cid": "c1"}
    with mock.patch.object(bluesky, "_xrpc", side_effect=[ref, bluesky.BlueskyError("down")]):
        with pytest.raises(bluesky.BlueskyError) as raised:
            bluesky.post_thread(SESSION, ["a", "b", "c"])
    assert raised.value.posted == [ref]


def test_post_url():
    assert (
        bluesky.post_url("o.bsky.social", "at://did:plc:abc/app.bsky.feed.post/3kabc")
        == "https://bsky.app/profile/o.bsky.social/post/3kabc"
    )


# -- bot label --


def test_label_is_added_and_the_profile_kept():
    existing = {"value": {"displayName": "Ouroboros", "description": "A bot."}}
    with mock.patch.object(bluesky, "_xrpc", side_effect=[existing, {}]) as xrpc:
        assert bluesky.ensure_bot_label(SESSION)
    record = xrpc.call_args_list[1].kwargs["body"]["record"]
    assert record["displayName"] == "Ouroboros"
    assert record["labels"]["values"] == [{"val": "bot"}]


def test_label_already_there_writes_nothing():
    existing = {"value": {"labels": {"values": [{"val": "bot"}]}}}
    with mock.patch.object(bluesky, "_xrpc", return_value=existing) as xrpc:
        assert bluesky.ensure_bot_label(SESSION)
    assert xrpc.call_count == 1


def test_missing_profile_is_created():
    missing = bluesky.BlueskyError("x")
    missing.__cause__ = _http_error(400)
    with mock.patch.object(bluesky, "_xrpc", side_effect=[missing, {}]) as xrpc:
        assert bluesky.ensure_bot_label(SESSION)
    assert xrpc.call_args_list[1].kwargs["body"]["record"]["labels"] == bluesky.BOT_LABEL


def test_failed_profile_read_does_not_overwrite_it():
    failed = bluesky.BlueskyError("x")
    failed.__cause__ = _http_error(502)
    with mock.patch.object(bluesky, "_xrpc", side_effect=[failed]) as xrpc:
        with pytest.raises(bluesky.BlueskyError):
            bluesky.ensure_bot_label(SESSION)
    assert xrpc.call_count == 1


# -- cross-post step --

POST = {"title": TITLE, "content": CONTENT}


def test_cross_post_records_the_root_and_labels_once():
    state, entry, notify = {}, {}, mock.MagicMock()
    refs = [{"uri": "at://did/app.bsky.feed.post/root", "cid": "c"}]
    with mock.patch.object(bluesky, "load_credentials", return_value=bluesky.Credentials("h", "p")), \
            mock.patch.object(bluesky, "create_session", return_value=SESSION), \
            mock.patch.object(bluesky, "ensure_bot_label", return_value=True) as label, \
            mock.patch.object(bluesky, "post_thread", return_value=refs):
        url = attention.cross_post_bluesky(state, POST, entry, notify)
        attention.cross_post_bluesky(state, POST, {}, notify)
    assert url == "https://bsky.app/profile/ouroboros.bsky.social/post/root"
    assert entry["bsky_uri"] == refs[0]["uri"]
    assert label.call_count == 1


def test_cross_post_without_credentials_does_nothing():
    with mock.patch.object(bluesky, "load_credentials", return_value=None), \
            mock.patch.object(bluesky, "create_session") as session:
        assert attention.cross_post_bluesky({}, POST, {}, mock.MagicMock()) is None
    session.assert_not_called()


def test_cross_post_never_raises():
    with mock.patch.object(bluesky, "load_credentials", side_effect=RuntimeError("boom")):
        assert attention.cross_post_bluesky({}, POST, {}, mock.MagicMock()) is None


def test_cut_short_thread_still_records_its_root():
    ref = {"uri": "at://did/app.bsky.feed.post/root", "cid": "c"}
    error = bluesky.BlueskyError("down")
    error.posted = [ref]
    entry = {}
    with mock.patch.object(bluesky, "load_credentials", return_value=bluesky.Credentials("h", "p")), \
            mock.patch.object(bluesky, "create_session", return_value=SESSION), \
            mock.patch.object(bluesky, "ensure_bot_label", return_value=True), \
            mock.patch.object(bluesky, "post_thread", side_effect=error):
        assert attention.cross_post_bluesky({}, POST, entry, mock.MagicMock()) is not None
    assert entry["bsky_uri"] == ref["uri"]


def test_setting_is_operator_only():
    from ouroboros import config_schema

    assert config_schema.unknown_key_error("enable_bluesky_posts") is None
    assert "enable_bluesky_posts" not in config_schema.COMMENT_SUGGESTIBLE_FIELDS
