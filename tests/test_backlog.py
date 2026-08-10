import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from unittest.mock import patch

from ouroboros.backlog import (
    organize_backlog,
    load_backlog,
    save_backlog,
    add_item,
    mark_done,
    mark_failed,
    get_pending,
    format_backlog_for_llm,
)
from pytest import fixture

class TestBacklog:
    @fixture(autouse=True)
    def setup_and_teardown(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.config_dir = self.tmp_dir / "config"
        self.config_dir.mkdir()
        yield
        shutil.rmtree(self.tmp_dir)

    def test_load_save_backlog(self):
        items = [{"id": "1", "task_type": "feat", "description": "test task"}]
        save_backlog(self.tmp_dir, items)

        loaded = load_backlog(self.tmp_dir)
        assert len(loaded) == 1
        assert loaded[0]["id"] == "1"
        assert loaded[0]["description"] == "test task"

    def test_load_empty_backlog(self):
        loaded = load_backlog(self.tmp_dir)
        assert loaded == []

    def test_add_item(self):
        entry = add_item(self.tmp_dir, "refactor", "refactor core logic", priority=8)
        assert entry["task_type"] == "refactor"
        assert entry["priority"] == 8
        assert entry["status"] == "pending"

        items = load_backlog(self.tmp_dir)
        assert len(items) == 1
        assert items[0]["id"] == entry["id"]

    def test_add_duplicate_item(self):
        desc = "unique description"
        add_item(self.tmp_dir, "feat", desc)
        add_item(self.tmp_dir, "feat", desc)

        items = load_backlog(self.tmp_dir)
        assert len(items) == 1

    def test_add_duplicate_item_done(self):
        desc = "repeat completed task"
        first = add_item(self.tmp_dir, "feat", desc)
        mark_done(self.tmp_dir, first["id"])

        second = add_item(self.tmp_dir, "feat", desc)

        items = load_backlog(self.tmp_dir)
        assert len(items) == 2
        assert items[0]["status"] == "done"
        assert items[1]["status"] == "pending"
        assert second["id"] != first["id"]

    def test_add_duplicate_item_abandoned(self):
        desc = "repeat abandoned task"
        first = add_item(self.tmp_dir, "fix", desc)
        for _ in range(3):
            mark_failed(self.tmp_dir, first["id"])

        second = add_item(self.tmp_dir, "fix", desc)

        items = load_backlog(self.tmp_dir)
        assert len(items) == 2
        assert items[0]["status"] == "abandoned"
        assert items[1]["status"] == "pending"
        assert second["id"] != first["id"]

    def test_mark_done(self):
        entry = add_item(self.tmp_dir, "fix", "fix bug")
        mark_done(self.tmp_dir, entry["id"])

        items = load_backlog(self.tmp_dir)
        assert items[0]["status"] == "done"
        assert "completed_at" in items[0]

    def test_mark_failed(self):
        entry = add_item(self.tmp_dir, "fix", "fix flakey test")
        item_id = entry["id"]

        mark_failed(self.tmp_dir, item_id)
        items = load_backlog(self.tmp_dir)
        assert items[0]["attempts"] == 1
        assert items[0]["status"] == "pending"

        mark_failed(self.tmp_dir, item_id)
        mark_failed(self.tmp_dir, item_id)
        items = load_backlog(self.tmp_dir)
        assert items[0]["attempts"] == 3
        assert items[0]["status"] == "abandoned"

    def test_get_pending_sorted(self):
        add_item(self.tmp_dir, "feat", "low pri", priority=1)
        add_item(self.tmp_dir, "feat", "high pri", priority=10)
        add_item(self.tmp_dir, "feat", "med pri", priority=5)

        pending = get_pending(self.tmp_dir)
        assert len(pending) == 3
        assert pending[0]["priority"] == 10
        assert pending[1]["priority"] == 5
        assert pending[2]["priority"] == 1

    def test_format_backlog_for_llm(self):
        items = [
            {"id": "1", "task_type": "feat", "description": "high", "priority": 10, "status": "pending", "attempts": 0},
            {"id": "2", "task_type": "fix", "description": "low", "priority": 1, "status": "pending", "attempts": 2},
            {"id": "3", "task_type": "docs", "description": "done", "priority": 5, "status": "done", "attempts": 0},
        ]
        formatted = format_backlog_for_llm(items)
        assert "### Improvement Backlog" in formatted
        assert "[P10] feat: high" in formatted
        assert "[P1] fix: low (attempts: 2)" in formatted
        assert "done" not in formatted  # completed items should be excluded


# -- centralised JSON IO -----------------------------------------------------

def test_load_backlog_missing_file(tmp_path):
    assert load_backlog(tmp_path) == []


def test_load_backlog_corrupt_file(tmp_path):
    path = tmp_path / "config" / "backlog.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")
    assert load_backlog(tmp_path) == []


def test_load_backlog_accepts_a_bare_list(tmp_path):
    """Older files stored the list directly rather than under "items"."""
    path = tmp_path / "config" / "backlog.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{"id": "1"}]))
    assert load_backlog(tmp_path) == [{"id": "1"}]


def test_save_backlog_creates_the_directory(tmp_path):
    save_backlog(tmp_path, [{"id": "1"}])
    assert (tmp_path / "config" / "backlog.json").exists()


def test_backlog_round_trip(tmp_path):
    items = [{"id": "a", "description": "x"}, {"id": "b", "description": "y"}]
    save_backlog(tmp_path, items)
    assert load_backlog(tmp_path) == items


def test_save_backlog_leaves_no_temp_file(tmp_path):
    save_backlog(tmp_path, [{"id": "1"}])
    leftovers = list((tmp_path / "config").glob("*.tmp"))
    assert leftovers == []


def test_unreadable_backlog_is_not_silently_replaced(tmp_path, monkeypatch):
    """Returning [] here would let add_item overwrite a real backlog."""
    from pathlib import Path as _Path

    path = tmp_path / "config" / "backlog.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"items": [{"id": "keep-me"}]}))
    original = path.read_text()

    def unreadable(*a, **kw):
        raise PermissionError("cannot read")

    monkeypatch.setattr(_Path, "open", unreadable)
    with pytest.raises(PermissionError):
        load_backlog(tmp_path)

    monkeypatch.undo()
    assert path.read_text() == original


def test_load_backlog_binary_garbage(tmp_path):
    """A crash can leave a file full of NULs, which never reaches the parser."""
    path = tmp_path / "config" / "backlog.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x00\x81\xfe" * 100)
    assert load_backlog(tmp_path) == []


@pytest.mark.parametrize("payload", ['{"items": null}', "null", '"a string"', "42"])
def test_load_backlog_non_list_payload(tmp_path, payload):
    path = tmp_path / "config" / "backlog.json"
    path.parent.mkdir(parents=True)
    path.write_text(payload)
    assert load_backlog(tmp_path) == []


# -- organize_backlog --------------------------------------------------------

def _item(item_id, *, task_type="fix_bug", description="d", priority=5,
          status="pending", **extra):
    base = {
        "id": item_id,
        "task_type": task_type,
        "description": description,
        "priority": priority,
        "status": status,
        "source": "test",
        "created_at": 1000.0,
        "attempts": 0,
    }
    base.update(extra)
    return base


def _client_returning(payload):
    """Patch chat_completion to return payload; organize_backlog imports it late."""
    return patch("ouroboros.llm.chat_completion", return_value=(payload, None))


def test_organize_backlog_no_op_on_empty(tmp_path):
    with patch("ouroboros.llm.chat_completion") as chat:
        organize_backlog(tmp_path, client=object())
    chat.assert_not_called()


def test_organize_backlog_updates_an_existing_item(tmp_path):
    save_backlog(tmp_path, [_item("a1", description="old", priority=5)])

    payload = json.dumps({"items": [
        {"id": "a1", "type": "refactor", "desc": "new wording", "priority": 9}
    ]})
    with _client_returning(payload):
        organize_backlog(tmp_path, client=object())

    items = load_backlog(tmp_path)
    assert len(items) == 1
    assert items[0]["id"] == "a1"
    assert items[0]["task_type"] == "refactor"
    assert items[0]["description"] == "new wording"
    assert items[0]["priority"] == 9
    # Untouched bookkeeping survives.
    assert items[0]["created_at"] == 1000.0
    assert items[0]["source"] == "test"


def test_organize_backlog_keeps_fields_the_model_omitted(tmp_path):
    save_backlog(tmp_path, [_item("a1", task_type="add_test", description="keep me",
                                  priority=3)])

    with _client_returning(json.dumps({"items": [{"id": "a1"}]})):
        organize_backlog(tmp_path, client=object())

    item = load_backlog(tmp_path)[0]
    assert item["task_type"] == "add_test"
    assert item["description"] == "keep me"
    assert item["priority"] == 3


def test_organize_backlog_creates_a_merged_epic(tmp_path):
    """An id the model invented becomes a new item, not an update."""
    save_backlog(tmp_path, [_item("a1"), _item("b2")])

    payload = json.dumps({"items": [
        {"id": "epic1", "type": "refactor", "desc": "Merge a1 and b2", "priority": 8}
    ]})
    with _client_returning(payload):
        organize_backlog(tmp_path, client=object())

    items = load_backlog(tmp_path)
    assert [i["id"] for i in items] == ["epic1"]
    assert items[0]["source"] == "backlog_organizer"
    assert items[0]["status"] == "pending"
    assert items[0]["attempts"] == 0


def test_organize_backlog_drops_pruned_items(tmp_path):
    """Anything the model leaves out of its reply is pruned."""
    save_backlog(tmp_path, [_item("a1"), _item("b2"), _item("c3")])

    with _client_returning(json.dumps({"items": [{"id": "a1"}]})):
        organize_backlog(tmp_path, client=object())

    assert [i["id"] for i in load_backlog(tmp_path)] == ["a1"]


def test_organize_backlog_never_touches_non_pending_items(tmp_path):
    """Done and failed history must survive an organiser that ignores it."""
    save_backlog(tmp_path, [
        _item("done1", status="done"),
        _item("failed1", status="failed"),
        _item("pending1"),
    ])

    with _client_returning(json.dumps({"items": [{"id": "pending1"}]})):
        organize_backlog(tmp_path, client=object())

    ids = {i["id"] for i in load_backlog(tmp_path)}
    assert ids == {"done1", "failed1", "pending1"}


def test_organize_backlog_only_sends_pending_items_to_the_model(tmp_path):
    save_backlog(tmp_path, [_item("done1", status="done"), _item("p1")])

    with patch("ouroboros.llm.chat_completion",
               return_value=(json.dumps({"items": [{"id": "p1"}]}), None)) as chat:
        organize_backlog(tmp_path, client=object())

    prompt = chat.call_args.args[2]
    assert "p1" in prompt
    assert "done1" not in prompt


def test_organize_backlog_tolerates_prose_around_the_json(tmp_path):
    save_backlog(tmp_path, [_item("a1", priority=1)])

    payload = 'Sure! Here you go:\n```json\n{"items": [{"id": "a1", "priority": 7}]}\n```\n'
    with _client_returning(payload):
        organize_backlog(tmp_path, client=object())

    assert load_backlog(tmp_path)[0]["priority"] == 7


@pytest.mark.parametrize("payload", ["", "not json at all", "{broken", "null"])
def test_organize_backlog_leaves_the_backlog_alone_on_bad_output(tmp_path, payload):
    """A malformed reply must not destroy the backlog."""
    original = [_item("a1"), _item("b2")]
    save_backlog(tmp_path, original)

    with _client_returning(payload):
        organize_backlog(tmp_path, client=object())

    assert load_backlog(tmp_path) == original


def test_organize_backlog_survives_a_model_error(tmp_path):
    original = [_item("a1")]
    save_backlog(tmp_path, original)

    with patch("ouroboros.llm.chat_completion", side_effect=RuntimeError("api down")):
        organize_backlog(tmp_path, client=object())

    assert load_backlog(tmp_path) == original


def test_organize_backlog_passes_the_model_through(tmp_path):
    save_backlog(tmp_path, [_item("a1")])

    with patch("ouroboros.llm.chat_completion",
               return_value=(json.dumps({"items": [{"id": "a1"}]}), None)) as chat:
        organize_backlog(tmp_path, client=object(), model="gpt-test")

    assert chat.call_args.kwargs["model"] == "gpt-test"


@pytest.mark.parametrize(
    "payload",
    [
        '{"items": []}',            # truncated reply is indistinguishable from "delete all"
        '{"other_key": [1]}',       # valid JSON, wrong shape
        '{"items": "not a list"}',
        '[{"id": "a1"}]',           # a bare list, not the documented object
    ],
)
def test_organize_backlog_refuses_to_empty_the_backlog(tmp_path, payload):
    """Omission is how this protocol prunes, so an empty list cannot be
    distinguished from a truncated reply -- and acting on it deletes every
    pending item."""
    original = [_item("a1"), _item("b2")]
    save_backlog(tmp_path, original)

    with _client_returning(payload):
        organize_backlog(tmp_path, client=object())

    assert load_backlog(tmp_path) == original


def test_organize_backlog_skips_a_backlog_with_nothing_pending(tmp_path):
    """A history of done and failed records is not something to organise."""
    save_backlog(tmp_path, [_item("d1", status="done"), _item("f1", status="failed")])

    with patch("ouroboros.llm.chat_completion") as chat:
        organize_backlog(tmp_path, client=object())

    chat.assert_not_called()
    assert {i["id"] for i in load_backlog(tmp_path)} == {"d1", "f1"}


def test_organize_backlog_tolerates_a_sparse_record(tmp_path):
    """A hand-edited or legacy record must not raise out of the organiser."""
    save_backlog(tmp_path, [{"id": "a1", "status": "pending"}])

    with _client_returning(json.dumps({"items": [{"id": "a1", "priority": 4}]})):
        organize_backlog(tmp_path, client=object())

    assert load_backlog(tmp_path)[0]["priority"] == 4


def test_format_backlog_for_llm_is_empty_without_pending_items():
    """Nothing pending means nothing to put in the prompt."""
    from ouroboros.backlog import format_backlog_for_llm

    assert format_backlog_for_llm([_item("d1", status="done")]) == ""


@pytest.mark.parametrize(
    "payload",
    [
        '{"items": [{}]}',                        # blank entry
        '{"items": [{"priority": 3}]}',           # no id, no description
        '{"items": [{"id": null, "desc": ""}]}',
        '{"items": ["a string"]}',                # not even an object
        '{"items": [{"id": "a1"}, {}]}',          # one good, one blank
    ],
)
def test_organize_backlog_rejects_unusable_entries(tmp_path, payload):
    """{"items": [{}]} passed the emptiness check and replaced the whole
    pending backlog with one blank task."""
    original = [_item("a1"), _item("b2")]
    save_backlog(tmp_path, original)

    with _client_returning(payload):
        organize_backlog(tmp_path, client=object())

    assert load_backlog(tmp_path) == original


def test_organize_backlog_tolerates_a_record_without_status(tmp_path):
    save_backlog(tmp_path, [{"id": "a1", "description": "d", "priority": 5}])

    with _client_returning(json.dumps({"items": [{"id": "n1", "desc": "new"}]})):
        organize_backlog(tmp_path, client=object())

    # No pending records at all, so there was nothing to organise.
    assert [i["id"] for i in load_backlog(tmp_path)] == ["a1"]


def test_organize_backlog_tolerates_a_record_without_an_id(tmp_path):
    """A record with no id must not be merged into by an entry with no id."""
    save_backlog(tmp_path, [{"status": "pending", "description": "keep", "priority": 5}])

    with _client_returning(json.dumps({"items": [{"desc": "brand new"}]})):
        organize_backlog(tmp_path, client=object())

    items = load_backlog(tmp_path)
    assert len(items) == 1
    assert items[0]["description"] == "brand new"
    assert items[0]["source"] == "backlog_organizer"


@pytest.mark.parametrize(
    "payload",
    [
        # Explicit nulls must not blank fields on an existing item.
        '{"items": [{"id": "a1", "type": null, "desc": "", "priority": null}]}',
        '{"items": [{"id": "a1", "priority": 0}]}',      # out of range
        '{"items": [{"id": "a1", "priority": 11}]}',
        '{"items": [{"id": "a1", "priority": "high"}]}',  # wrong type
        '{"items": [{"id": "a1", "type": 42}]}',
        '{"items": [{"id": "a1", "desc": 42}]}',
    ],
)
def test_organize_backlog_rejects_bad_field_values(tmp_path, payload):
    """A null priority makes get_pending raise while sorting."""
    original = [_item("a1", description="keep", priority=5)]
    save_backlog(tmp_path, original)

    with _client_returning(payload):
        organize_backlog(tmp_path, client=object())

    assert load_backlog(tmp_path) == original


def test_organize_backlog_rejects_duplicate_ids(tmp_path):
    """mark_done stops at the first match, so a duplicate stays pending and
    gets worked twice."""
    original = [_item("a1"), _item("b2")]
    save_backlog(tmp_path, original)

    payload = json.dumps({"items": [{"id": "a1"}, {"id": "a1", "priority": 9}]})
    with _client_returning(payload):
        organize_backlog(tmp_path, client=object())

    assert load_backlog(tmp_path) == original


def test_organize_backlog_will_not_let_a_new_item_declare_itself_done(tmp_path):
    """status="done" would delete the omitted records and leave nothing
    pending to replace them."""
    save_backlog(tmp_path, [_item("a1")])

    payload = json.dumps({"items": [{"desc": "replacement", "status": "done"}]})
    with _client_returning(payload):
        organize_backlog(tmp_path, client=object())

    items = load_backlog(tmp_path)
    assert len(items) == 1
    assert items[0]["status"] == "pending"


def test_organize_backlog_will_not_rewrite_a_completed_record(tmp_path):
    """An invented id colliding with history must not mutate it."""
    done = _item("done1", status="done", description="original", priority=2)
    save_backlog(tmp_path, [done, _item("p1")])

    payload = json.dumps({"items": [{"id": "done1", "desc": "hijacked", "priority": 9}]})
    with _client_returning(payload):
        organize_backlog(tmp_path, client=object())

    items = load_backlog(tmp_path)
    survivor = next(i for i in items if i["id"] == "done1")
    assert survivor == done, "the completed record must be untouched"
    # The organizer's entry became a new pending task under a fresh id.
    fresh = [i for i in items if i["id"] != "done1"]
    assert len(fresh) == 1
    assert fresh[0]["description"] == "hijacked"
    assert fresh[0]["status"] == "pending"


def test_organize_backlog_applies_only_supplied_fields(tmp_path):
    save_backlog(tmp_path, [_item("a1", task_type="add_test",
                                  description="keep", priority=3)])

    with _client_returning(json.dumps({"items": [{"id": "a1", "priority": 8}]})):
        organize_backlog(tmp_path, client=object())

    item = load_backlog(tmp_path)[0]
    assert item["priority"] == 8
    assert item["task_type"] == "add_test"
    assert item["description"] == "keep"
