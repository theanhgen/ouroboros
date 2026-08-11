import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from unittest.mock import patch

from ouroboros.backlog import (
    mark_done,
    mark_failed,
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


def _response(keep=(), delete=(), merge=()):
    return json.dumps({
        "keep": list(keep), "delete": list(delete), "merge": list(merge),
    })


def _client_returning(payload):
    """Patch chat_completion; organize_backlog imports it late."""
    return patch("ouroboros.llm.chat_completion", return_value=(payload, None))


# -- every id must get an explicit outcome -----------------------------------

def test_a_complete_response_is_applied(tmp_path):
    save_backlog(tmp_path, [_item("a1"), _item("b2"), _item("c3"), _item("d4")])

    payload = _response(
        keep=[{"id": "a1", "priority": 9}],
        delete=[{"id": "b2", "reason": "duplicate of a1"}],
        merge=[{"sources": ["c3", "d4"], "desc": "merged", "priority": 7}],
    )
    with _client_returning(payload):
        result = organize_backlog(tmp_path, client=object())

    assert result.ok
    assert (result.kept, result.deleted, result.merged) == (1, 1, 1)

    items = load_backlog(tmp_path)
    by_desc = {i["description"]: i for i in items}
    assert set(by_desc) == {"d", "merged"}
    assert by_desc["d"]["id"] == "a1"
    assert by_desc["d"]["priority"] == 9
    assert by_desc["merged"]["source"] == "backlog_organizer"
    assert by_desc["merged"]["status"] == "pending"


def test_an_incomplete_response_is_rejected_whole(tmp_path):
    """The old protocol pruned by omission, so a truncated reply was
    indistinguishable from a deliberate deletion of everything missing."""
    original = [_item("a1"), _item("b2"), _item("c3")]
    save_backlog(tmp_path, original)

    with _client_returning(_response(keep=[{"id": "a1"}])):
        result = organize_backlog(tmp_path, client=object())

    assert not result.ok
    assert "b2" in result.reason and "c3" in result.reason
    assert load_backlog(tmp_path) == original


def test_an_id_in_two_sections_is_rejected(tmp_path):
    original = [_item("a1")]
    save_backlog(tmp_path, original)

    payload = _response(keep=[{"id": "a1"}], delete=[{"id": "a1", "reason": "x"}])
    with _client_returning(payload):
        result = organize_backlog(tmp_path, client=object())

    assert not result.ok
    assert load_backlog(tmp_path) == original


def test_an_unknown_id_is_rejected(tmp_path):
    """Only pending items go into the prompt, so any other id is invented."""
    original = [_item("a1")]
    save_backlog(tmp_path, original)

    payload = _response(keep=[{"id": "a1"}, {"id": "invented"}])
    with _client_returning(payload):
        result = organize_backlog(tmp_path, client=object())

    assert not result.ok
    assert "invented" in result.reason
    assert load_backlog(tmp_path) == original


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not json at all",
        "{broken",
        "null",
        '{"keep": "not a list"}',
        '{"keep": ["a string"]}',
        '{"keep": [{}]}',                                    # no id
        '{"merge": [{"sources": [], "desc": "x"}]}',         # no sources
        '{"merge": [{"sources": ["a1"], "desc": ""}]}',      # no description
        '{"keep": [{"id": "a1", "priority": 0}]}',           # out of range
        '{"keep": [{"id": "a1", "priority": "high"}]}',      # wrong type
    ],
)
def test_a_malformed_response_leaves_the_backlog_alone(tmp_path, payload):
    original = [_item("a1")]
    save_backlog(tmp_path, original)

    with _client_returning(payload):
        result = organize_backlog(tmp_path, client=object())

    assert not result.ok
    assert load_backlog(tmp_path) == original


def test_prose_around_the_json_is_tolerated(tmp_path):
    save_backlog(tmp_path, [_item("a1", priority=1)])

    payload = 'Sure!\n```json\n' + _response(keep=[{"id": "a1", "priority": 7}]) + '\n```\n'
    with _client_returning(payload):
        assert organize_backlog(tmp_path, client=object()).ok

    assert load_backlog(tmp_path)[0]["priority"] == 7


# -- what the organizer may and may not touch --------------------------------

def test_only_supplied_fields_are_applied(tmp_path):
    save_backlog(tmp_path, [_item("a1", task_type="add_test",
                                  description="keep", priority=3)])

    with _client_returning(_response(keep=[{"id": "a1", "priority": 8}])):
        organize_backlog(tmp_path, client=object())

    item = load_backlog(tmp_path)[0]
    assert item["priority"] == 8
    assert item["task_type"] == "add_test"
    assert item["description"] == "keep"


def test_non_pending_items_are_never_sent_or_touched(tmp_path):
    save_backlog(tmp_path, [
        _item("done1", status="done"),
        _item("failed1", status="abandoned"),
        _item("p1"),
    ])

    with patch("ouroboros.llm.chat_completion",
               return_value=(_response(keep=[{"id": "p1"}]), None)) as chat:
        organize_backlog(tmp_path, client=object())

    prompt = chat.call_args.args[2]
    assert "done1" not in prompt and "failed1" not in prompt
    assert {i["id"] for i in load_backlog(tmp_path)} == {"done1", "failed1", "p1"}


def test_a_backlog_with_nothing_pending_is_skipped(tmp_path):
    save_backlog(tmp_path, [_item("d1", status="done")])

    with patch("ouroboros.llm.chat_completion") as chat:
        result = organize_backlog(tmp_path, client=object())

    chat.assert_not_called()
    assert result.ok and result.kept == 0


def test_a_record_without_an_id_is_left_alone(tmp_path):
    """It cannot be referenced in the response, so it cannot be given an
    outcome -- and must not block the run or be deleted for that."""
    save_backlog(tmp_path, [
        {"status": "pending", "description": "no id", "priority": 5},
        _item("a1"),
    ])

    with _client_returning(_response(keep=[{"id": "a1"}])):
        assert organize_backlog(tmp_path, client=object()).ok

    assert {i.get("description") for i in load_backlog(tmp_path)} == {"no id", "d"}


def test_a_sparse_record_does_not_raise(tmp_path):
    save_backlog(tmp_path, [{"id": "a1", "status": "pending"}])

    with _client_returning(_response(keep=[{"id": "a1", "priority": 4}])):
        assert organize_backlog(tmp_path, client=object()).ok

    assert load_backlog(tmp_path)[0]["priority"] == 4


def test_the_model_call_failing_leaves_the_backlog_alone(tmp_path):
    original = [_item("a1")]
    save_backlog(tmp_path, original)

    with patch("ouroboros.llm.chat_completion", side_effect=RuntimeError("api down")):
        result = organize_backlog(tmp_path, client=object())

    assert not result.ok
    assert load_backlog(tmp_path) == original


# -- concurrency: live state wins over the organizer's stale view ------------

def test_a_concurrent_add_survives(tmp_path):
    save_backlog(tmp_path, [_item("a1")])

    def slow_model(*a, **k):
        add_item(tmp_path, "add_test", "added during the model call")
        return (_response(keep=[{"id": "a1"}]), None)

    with patch("ouroboros.llm.chat_completion", side_effect=slow_model):
        organize_backlog(tmp_path, client=object())

    assert "added during the model call" in {
        i["description"] for i in load_backlog(tmp_path)
    }


def test_a_concurrent_failure_count_survives_a_keep(tmp_path):
    save_backlog(tmp_path, [_item("a1")])

    def slow_model(*a, **k):
        mark_failed(tmp_path, "a1")
        return (_response(keep=[{"id": "a1", "priority": 9}]), None)

    with patch("ouroboros.llm.chat_completion", side_effect=slow_model):
        organize_backlog(tmp_path, client=object())

    item = load_backlog(tmp_path)[0]
    assert item["attempts"] == 1, "bookkeeping is never written by the organizer"
    assert item["priority"] == 9, "an unrelated field change still applies"


def test_a_concurrent_completion_is_not_deleted(tmp_path):
    save_backlog(tmp_path, [_item("a1"), _item("b2")])

    def slow_model(*a, **k):
        mark_done(tmp_path, "b2")
        return (_response(keep=[{"id": "a1"}],
                          delete=[{"id": "b2", "reason": "stale"}]), None)

    with patch("ouroboros.llm.chat_completion", side_effect=slow_model):
        organize_backlog(tmp_path, client=object())

    by_id = {i["id"]: i for i in load_backlog(tmp_path)}
    assert by_id["b2"]["status"] == "done", "finished work must not be deleted"


def test_a_merge_still_applies_after_a_concurrent_failure(tmp_path):
    """A failure increment does not make two tasks stop being duplicates."""
    save_backlog(tmp_path, [_item("a1"), _item("b2")])

    def slow_model(*a, **k):
        mark_failed(tmp_path, "a1")
        return (_response(merge=[{"sources": ["a1", "b2"], "desc": "merged"}]), None)

    with patch("ouroboros.llm.chat_completion", side_effect=slow_model):
        organize_backlog(tmp_path, client=object())

    assert {i["description"] for i in load_backlog(tmp_path)} == {"merged"}


def test_a_merge_does_not_fire_if_another_writer_removed_the_sources(tmp_path):
    """Inferring "removed" from absence would let this stale merge resurrect
    work another writer had already replaced."""
    save_backlog(tmp_path, [_item("a1"), _item("b2")])

    def slow_model(*a, **k):
        save_backlog(tmp_path, [_item("replacement", description="already merged")])
        return (_response(merge=[{"sources": ["a1", "b2"], "desc": "merged"}]), None)

    with patch("ouroboros.llm.chat_completion", side_effect=slow_model):
        organize_backlog(tmp_path, client=object())

    assert {i["description"] for i in load_backlog(tmp_path)} == {"already merged"}


def test_a_merge_does_not_fire_if_the_sources_completed(tmp_path):
    save_backlog(tmp_path, [_item("a1"), _item("b2")])

    def slow_model(*a, **k):
        mark_done(tmp_path, "a1")
        mark_done(tmp_path, "b2")
        return (_response(merge=[{"sources": ["a1", "b2"], "desc": "merged"}]), None)

    with patch("ouroboros.llm.chat_completion", side_effect=slow_model):
        organize_backlog(tmp_path, client=object())

    items = load_backlog(tmp_path)
    assert {i["status"] for i in items} == {"done"}
    assert "merged" not in {i["description"] for i in items}


def test_duplicate_pending_ids_abort_the_run(tmp_path):
    """A dict keyed by id would hide one of them from the model, and then a
    single decision would delete both."""
    original = [_item("dup"), _item("dup", description="other")]
    save_backlog(tmp_path, original)

    with patch("ouroboros.llm.chat_completion") as chat:
        result = organize_backlog(tmp_path, client=object())

    chat.assert_not_called()
    assert not result.ok
    assert "dup" in result.reason
    assert load_backlog(tmp_path) == original


# -- the result is reportable ------------------------------------------------

def test_result_summary_on_success():
    from ouroboros.backlog import OrganizeResult

    summary = OrganizeResult(ok=True, kept=2, deleted=1, merged=3).summary()
    assert "2 kept" in summary and "1 deleted" in summary and "3 merged" in summary


def test_result_summary_on_failure():
    from ouroboros.backlog import OrganizeResult

    assert "unchanged" in OrganizeResult(ok=False, reason="bad").summary()


def test_a_merge_source_repeated_in_another_section_is_rejected(tmp_path):
    original = [_item("a1"), _item("b2")]
    save_backlog(tmp_path, original)

    payload = _response(
        keep=[{"id": "a1"}],
        merge=[{"sources": ["a1", "b2"], "desc": "merged"}],
    )
    with _client_returning(payload):
        result = organize_backlog(tmp_path, client=object())

    assert not result.ok
    assert "a1" in result.reason
    assert load_backlog(tmp_path) == original


def test_a_null_section_is_treated_as_empty(tmp_path):
    """A model that omits a section entirely, or sends null, still has to
    account for every id in the ones it does send."""
    save_backlog(tmp_path, [_item("a1")])

    with _client_returning(json.dumps({"keep": [{"id": "a1"}], "delete": None})):
        assert organize_backlog(tmp_path, client=object()).ok


def test_pending_records_without_ids_only(tmp_path):
    """Nothing addressable, so there is nothing for the organizer to do."""
    save_backlog(tmp_path, [{"status": "pending", "description": "no id"}])

    with patch("ouroboros.llm.chat_completion") as chat:
        result = organize_backlog(tmp_path, client=object())

    chat.assert_not_called()
    assert result.ok
    assert len(load_backlog(tmp_path)) == 1


def test_a_write_failure_is_reported_not_raised(tmp_path, monkeypatch):
    save_backlog(tmp_path, [_item("a1")])

    monkeypatch.setattr(
        "ouroboros.backlog._update_backlog",
        lambda root, mutate: (_ for _ in ()).throw(OSError("disk full")),
    )

    with _client_returning(_response(keep=[{"id": "a1"}])):
        result = organize_backlog(tmp_path, client=object())

    assert not result.ok
    assert "disk full" in result.reason


def test_a_keep_for_a_record_deleted_meanwhile_is_skipped(tmp_path):
    save_backlog(tmp_path, [_item("a1"), _item("b2")])

    def slow_model(*a, **k):
        save_backlog(tmp_path, [_item("b2")])  # a1 removed by another writer
        return (_response(keep=[{"id": "a1", "priority": 9},
                                {"id": "b2"}]), None)

    with patch("ouroboros.llm.chat_completion", side_effect=slow_model):
        organize_backlog(tmp_path, client=object())

    assert [i["id"] for i in load_backlog(tmp_path)] == ["b2"]


# -- restored: not organizer tests, kept from before the protocol change -----

def test_format_backlog_for_llm_is_empty_without_pending_items():
    """Nothing pending means nothing to put in the prompt."""
    from ouroboros.backlog import format_backlog_for_llm

    assert format_backlog_for_llm([_item("d1", status="done")]) == ""


@pytest.mark.parametrize(
    "mutator",
    [
        lambda root: add_item(root, "fix_bug", "new"),
        lambda root: mark_done(root, "old"),
        lambda root: mark_failed(root, "old"),
    ],
)
def test_mutators_accept_a_legacy_bare_list_file(tmp_path, mutator):
    """load_backlog supports it, so the mutators must too -- calling .get on a
    list raised AttributeError."""
    path = tmp_path / "config" / "backlog.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([_item("old")]))

    mutator(tmp_path)

    assert any(i["id"] == "old" for i in load_backlog(tmp_path))


def test_a_merge_is_all_or_nothing(tmp_path):
    """Removing only the surviving sources would leave the completed one in
    place and add a replacement that duplicates it."""
    save_backlog(tmp_path, [_item("a1"), _item("b2")])

    def slow_model(*a, **k):
        mark_done(tmp_path, "a1")
        return (_response(merge=[{"sources": ["a1", "b2"], "desc": "merged"}]), None)

    with patch("ouroboros.llm.chat_completion", side_effect=slow_model):
        organize_backlog(tmp_path, client=object())

    by_id = {i["id"]: i for i in load_backlog(tmp_path)}
    assert set(by_id) == {"a1", "b2"}, "neither source may be removed"
    assert by_id["a1"]["status"] == "done"
    assert "merged" not in {i["description"] for i in by_id.values()}


def test_a_delete_is_skipped_if_the_task_changed_meanwhile(tmp_path):
    """A concurrent edit may have made a "duplicate" distinct."""
    save_backlog(tmp_path, [_item("a1"), _item("b2")])

    def slow_model(*a, **k):
        items = load_backlog(tmp_path)
        for i in items:
            if i["id"] == "b2":
                i["description"] = "actually a different thing now"
        save_backlog(tmp_path, items)
        return (_response(keep=[{"id": "a1"}],
                          delete=[{"id": "b2", "reason": "duplicate of a1"}]), None)

    with patch("ouroboros.llm.chat_completion", side_effect=slow_model):
        organize_backlog(tmp_path, client=object())

    by_id = {i["id"]: i for i in load_backlog(tmp_path)}
    assert "b2" in by_id
    assert by_id["b2"]["description"] == "actually a different thing now"


def test_a_merge_is_skipped_if_a_source_changed_meanwhile(tmp_path):
    save_backlog(tmp_path, [_item("a1"), _item("b2")])

    def slow_model(*a, **k):
        items = load_backlog(tmp_path)
        for i in items:
            if i["id"] == "a1":
                i["priority"] = 10
        save_backlog(tmp_path, items)
        return (_response(merge=[{"sources": ["a1", "b2"], "desc": "merged"}]), None)

    with patch("ouroboros.llm.chat_completion", side_effect=slow_model):
        organize_backlog(tmp_path, client=object())

    assert {i["id"] for i in load_backlog(tmp_path)} == {"a1", "b2"}


@pytest.mark.parametrize(
    "sources", [["a1"], ["a1", "a1"]],
)
def test_a_singleton_merge_is_rejected(tmp_path, sources):
    """It combines nothing -- it just replaces the task with a fresh id and
    attempts back to zero, slipping past the abandonment rule."""
    original = [_item("a1", attempts=2)]
    save_backlog(tmp_path, original)

    payload = _response(merge=[{"sources": sources, "desc": "not really a merge"}])
    with _client_returning(payload):
        result = organize_backlog(tmp_path, client=object())

    assert not result.ok
    assert "two distinct sources" in result.reason
    assert load_backlog(tmp_path) == original


def test_a_merge_of_unchanged_sources_still_applies(tmp_path):
    """The guards must not make a legitimate merge impossible."""
    save_backlog(tmp_path, [_item("a1"), _item("b2"), _item("c3")])

    payload = _response(
        keep=[{"id": "c3"}],
        merge=[{"sources": ["a1", "b2"], "desc": "merged", "priority": 8}],
    )
    with _client_returning(payload):
        assert organize_backlog(tmp_path, client=object()).ok

    items = load_backlog(tmp_path)
    assert {i["description"] for i in items} == {"d", "merged"}
    merged = next(i for i in items if i["description"] == "merged")
    assert merged["priority"] == 8


def test_a_keep_does_not_overwrite_a_concurrent_edit(tmp_path):
    """The organizer's version of these fields is stale once someone else has
    rewritten them."""
    save_backlog(tmp_path, [_item("a1")])

    def slow_model(*a, **k):
        items = load_backlog(tmp_path)
        items[0]["description"] = "edited by someone else"
        save_backlog(tmp_path, items)
        return (_response(keep=[{"id": "a1", "desc": "stale rewrite"}]), None)

    with patch("ouroboros.llm.chat_completion", side_effect=slow_model):
        organize_backlog(tmp_path, client=object())

    assert load_backlog(tmp_path)[0]["description"] == "edited by someone else"


def test_an_id_shared_with_a_completed_record_aborts_the_run(tmp_path):
    """Reconciliation removes by id, so deleting the pending one would take
    the completed record with it."""
    original = [_item("x", status="done"), _item("x")]
    save_backlog(tmp_path, original)

    with patch("ouroboros.llm.chat_completion") as chat:
        result = organize_backlog(tmp_path, client=object())

    chat.assert_not_called()
    assert not result.ok
    assert "x" in result.reason
    assert len(load_backlog(tmp_path)) == 2
