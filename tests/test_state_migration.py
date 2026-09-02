"""Tests for the one-way JSON -> SQLite history backfill."""

import json

import pytest

from ouroboros.state_migration import MigrationReport, migrate_json_history
from ouroboros.storage import OuroborosStorage


@pytest.fixture
def store(tmp_path):
    return OuroborosStorage(db_path=tmp_path / "ouroboros.db")


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def _comment(i):
    return {"ts": float(i), "post_id": f"p{i}", "comment": f"text {i}", "title": "t"}


def _improvement(i):
    return {"task_id": f"t{i}", "timestamp": float(i), "outcome": "success",
            "task_type": "fix_bug", "description": "d", "test_delta": {},
            "pr_url": "", "feedback": ""}


def _paths(tmp_path):
    return {
        "state_path": tmp_path / "state.json",
        "history_path": tmp_path / "improvement_history.json",
        "kb_path": tmp_path / "knowledge_base.json",
    }


# -- the happy path ----------------------------------------------------------

def test_migrates_all_three_collections(tmp_path, store):
    paths = _paths(tmp_path)
    _write(paths["state_path"], {"comment_history": [_comment(1), _comment(2)]})
    _write(paths["history_path"], [_improvement(1)])
    _write(paths["kb_path"], {"entries": [{"insight": "a"}, {"insight": "b"}]})

    report = migrate_json_history(tmp_path, store, **paths)

    assert (report.comments, report.improvements, report.knowledge) == (2, 1, 2)
    assert store.comment_count() == 2
    assert store.improvement_count() == 1
    assert store.knowledge_count() == 2


def test_is_idempotent(tmp_path, store):
    """Runs on every start, so a second pass must add nothing."""
    paths = _paths(tmp_path)
    _write(paths["state_path"], {"comment_history": [_comment(1), _comment(2)]})
    _write(paths["history_path"], [_improvement(1)])

    first = migrate_json_history(tmp_path, store, **paths)
    second = migrate_json_history(tmp_path, store, **paths)

    assert first.total == 3
    assert second.total == 0
    assert store.comment_count() == 2
    assert store.improvement_count() == 1


def test_comments_dedupe_without_a_comment_id(tmp_path, store):
    """comment_id is null on every historical record.

    A UNIQUE over (post_id, comment_id) does not dedupe those, because SQLite
    treats each NULL as distinct -- a re-run would insert all of them again.
    """
    paths = _paths(tmp_path)
    records = [{"ts": 1.0, "post_id": "p1", "comment_id": None, "comment": "x"}]
    _write(paths["state_path"], {"comment_history": records})

    migrate_json_history(tmp_path, store, **paths)
    migrate_json_history(tmp_path, store, **paths)

    assert store.comment_count() == 1


def test_does_not_delete_the_source_files(tmp_path, store):
    """The JSON files are the rollback path."""
    paths = _paths(tmp_path)
    _write(paths["state_path"], {"comment_history": [_comment(1)]})
    _write(paths["history_path"], [_improvement(1)])
    _write(paths["kb_path"], {"entries": [{"insight": "a"}]})

    migrate_json_history(tmp_path, store, **paths)

    for path in paths.values():
        assert path.exists(), path


def test_preserves_the_full_record(tmp_path, store):
    paths = _paths(tmp_path)
    record = {"ts": 1.0, "post_id": "p1", "comment": "body", "title": "T",
              "content": "original post", "extra_field": [1, 2, 3]}
    _write(paths["state_path"], {"comment_history": [record]})

    migrate_json_history(tmp_path, store, **paths)

    assert store.get_comment_history()[0] == record


def test_preserves_the_json_list_order_exactly(tmp_path, store):
    """Append order, not timestamp order: the JSON list was the record of
    sequence, and a clock correction must not reshuffle history."""
    paths = _paths(tmp_path)
    _write(paths["state_path"],
           {"comment_history": [_comment(3), _comment(1), _comment(2)]})

    migrate_json_history(tmp_path, store, **paths)

    assert [c["ts"] for c in store.get_comment_history()] == [3.0, 1.0, 2.0]


# -- missing and malformed input ---------------------------------------------

def test_missing_files_are_not_an_error(tmp_path, store):
    report = migrate_json_history(tmp_path, store, **_paths(tmp_path))
    assert report.total == 0
    assert report.skipped == []


def test_corrupt_file_does_not_abort_the_rest(tmp_path, store):
    """One unreadable file must not block the other two."""
    paths = _paths(tmp_path)
    paths["state_path"].write_text("{ not json")
    _write(paths["history_path"], [_improvement(1)])

    report = migrate_json_history(tmp_path, store, **paths)

    assert report.improvements == 1


def test_non_object_records_are_skipped_not_fatal(tmp_path, store):
    paths = _paths(tmp_path)
    _write(paths["state_path"],
           {"comment_history": ["a string", _comment(1), None]})

    report = migrate_json_history(tmp_path, store, **paths)

    assert report.comments == 1
    assert len(report.skipped) == 2
    assert store.comment_count() == 1


def test_wrong_shaped_state_is_tolerated(tmp_path, store):
    paths = _paths(tmp_path)
    _write(paths["state_path"], ["not", "a", "dict"])
    report = migrate_json_history(tmp_path, store, **paths)
    assert report.comments == 0


def test_state_without_comment_history(tmp_path, store):
    paths = _paths(tmp_path)
    _write(paths["state_path"], {"last_check": 123})
    assert migrate_json_history(tmp_path, store, **paths).comments == 0


def test_explicitly_null_collections_are_tolerated(tmp_path, store):
    """A key present with a null value is not the same as a missing key.

    .get(key, []) returns the null, so the whole backfill used to abort with
    TypeError instead of skipping the one empty section.
    """
    paths = _paths(tmp_path)
    _write(paths["state_path"], {"comment_history": None})
    _write(paths["kb_path"], {"entries": None})
    _write(paths["history_path"], [_improvement(1)])

    report = migrate_json_history(tmp_path, store, **paths)

    assert (report.comments, report.knowledge) == (0, 0)
    assert report.improvements == 1


# -- report ------------------------------------------------------------------

def test_report_summary_mentions_every_collection():
    report = MigrationReport(comments=2, improvements=3, knowledge=4)
    summary = report.summary()
    assert "2 comments" in summary
    assert "3 improvements" in summary
    assert "4 knowledge entries" in summary
    assert report.total == 9


def test_report_summary_mentions_skipped_records():
    report = MigrationReport(skipped=["bad record"])
    assert "1 records skipped" in report.summary()


# -- the collections are actually cut over, not just snapshotted -------------

def test_comments_written_after_startup_are_visible_to_readers(tmp_path, monkeypatch):
    """A backfill that nothing reads or writes is a stale snapshot, not a
    migration -- it leaves two sources of truth."""
    from ouroboros import moltbook

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(moltbook, "_state_path", lambda: str(state_file))

    state = moltbook.load_state()
    moltbook.record_comment(state, {"ts": 1.0, "post_id": "p1", "comment": "written now"})
    moltbook.save_state(state)

    # A fresh load, as the next cycle would do.
    reloaded = moltbook.load_state()
    assert [c["comment"] for c in reloaded["comment_history"]] == ["written now"]


def test_comment_history_is_no_longer_persisted_in_state_json(tmp_path, monkeypatch):
    """102 KB of a 169 KB file, rewritten every cycle."""
    import json as _json

    from ouroboros import moltbook

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(moltbook, "_state_path", lambda: str(state_file))

    state = moltbook.load_state()
    moltbook.record_comment(state, {"ts": 1.0, "post_id": "p1", "comment": "x"})
    moltbook.save_state(state)

    assert "comment_history" not in _json.loads(state_file.read_text())


def test_legacy_comments_in_state_json_are_imported(tmp_path, monkeypatch):
    import json as _json

    from ouroboros import moltbook

    state_file = tmp_path / "state.json"
    state_file.write_text(_json.dumps({
        "comment_history": [{"ts": 1.0, "post_id": "p1", "comment": "legacy"}],
        "last_check": 123,
    }))
    monkeypatch.setattr(moltbook, "_state_path", lambda: str(state_file))

    state = moltbook.load_state()

    assert [c["comment"] for c in state["comment_history"]] == ["legacy"]
    assert state["last_check"] == 123


def test_knowledge_entries_written_after_startup_are_visible(tmp_path):
    from ouroboros.knowledge_base import add_entries, load_kb

    path = str(tmp_path / "kb.json")
    add_entries([{"ts": 1, "insight": "written now"}], path)

    assert [e["insight"] for e in load_kb(path)["entries"]] == ["written now"]


def test_improvements_written_after_startup_are_visible(tmp_path):
    from ouroboros.evaluation import EvaluationRecord, load_history
    from ouroboros.storage import OuroborosStorage

    record = EvaluationRecord(
        task_id="t1", task_type="fix_bug", description="d",
        outcome="success", timestamp=1.0,
    )
    OuroborosStorage(
        db_path=tmp_path / "config" / "ouroboros.db"
    ).append_improvement(record.to_dict())

    assert [r.task_id for r in load_history(tmp_path)] == ["t1"]


# -- the rollback story has to actually be true ------------------------------

def test_state_json_legacy_comments_survive_the_first_save(tmp_path, monkeypatch):
    """save_state stops writing comment_history, so without a snapshot the
    first normal cycle would erase the pre-cutover comments it claims to
    preserve for rollback."""
    import json as _json

    from ouroboros import moltbook

    state_file = tmp_path / "state.json"
    state_file.write_text(_json.dumps({
        "comment_history": [{"ts": 1.0, "post_id": "p1", "comment": "legacy"}],
        "last_check": 1,
    }))
    monkeypatch.setattr(moltbook, "_state_path", lambda: str(state_file))

    state = moltbook.load_state()
    moltbook.save_state(state)

    assert "comment_history" not in _json.loads(state_file.read_text())
    snapshot = _json.loads((tmp_path / "state.json.pre-sqlite").read_text())
    assert [c["comment"] for c in snapshot["comment_history"]] == ["legacy"]


def test_knowledge_json_legacy_entries_survive_a_summary_refresh(tmp_path):
    """save_kb writes only the cache, so the next refresh would drop them."""
    import json as _json

    from ouroboros.knowledge_base import load_kb, save_kb

    kb_file = tmp_path / "kb.json"
    kb_file.write_text(_json.dumps({
        "entries": [{"ts": 1, "insight": "legacy"}],
        "summary_cache": "", "summary_updated_at": 0,
    }))

    kb = load_kb(str(kb_file))
    save_kb({"summary_cache": "new", "summary_updated_at": 2}, str(kb_file))

    assert "entries" not in _json.loads(kb_file.read_text())
    snapshot = _json.loads((tmp_path / "kb.json.pre-sqlite").read_text())
    assert [e["insight"] for e in snapshot["entries"]] == ["legacy"]


def test_snapshot_is_taken_once_and_not_overwritten(tmp_path, store):
    from ouroboros.state_migration import freeze_rollback_snapshot

    source = tmp_path / "state.json"
    source.write_text('{"original": true}')

    freeze_rollback_snapshot(source)
    source.write_text('{"changed": true}')
    freeze_rollback_snapshot(source)

    assert (tmp_path / "state.json.pre-sqlite").read_text() == '{"original": true}'


def test_snapshot_of_a_missing_file_is_a_no_op(tmp_path):
    from ouroboros.state_migration import freeze_rollback_snapshot

    assert freeze_rollback_snapshot(tmp_path / "nope.json") is None


# -- a public comment must not be lost if the database write fails -----------

def test_a_comment_survives_a_storage_failure(tmp_path, monkeypatch):
    """The comment is already public; save_state drops the in-memory list, so
    without an outbox the local record would be gone after a restart."""
    from ouroboros import moltbook

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(moltbook, "_state_path", lambda: str(state_file))

    state = moltbook.load_state()

    class Broken:
        def append_comment(self, entry):
            raise OSError("disk full")

    monkeypatch.setattr(moltbook, "_comment_storage", lambda: Broken())
    moltbook.record_comment(state, {"ts": 1.0, "post_id": "p1", "comment": "posted"})
    moltbook.save_state(state)

    # It is queued in the file that does get written.
    import json as _json
    assert _json.loads(state_file.read_text())["comment_outbox"][0]["comment"] == "posted"


def test_the_outbox_is_drained_on_the_next_load(tmp_path, monkeypatch):
    import json as _json

    from ouroboros import moltbook

    state_file = tmp_path / "state.json"
    state_file.write_text(_json.dumps({
        "comment_outbox": [{"ts": 1.0, "post_id": "p1", "comment": "recovered"}],
    }))
    monkeypatch.setattr(moltbook, "_state_path", lambda: str(state_file))

    state = moltbook.load_state()

    assert state["comment_outbox"] == []
    assert [c["comment"] for c in state["comment_history"]] == ["recovered"]


def test_both_knowledge_import_paths_agree_on_identity(tmp_path, store):
    """load_kb and the startup migration both import legacy entries. Different
    fingerprints would mean whichever runs second inserts them again."""
    import json as _json

    from ouroboros.knowledge_base import load_kb

    kb_file = tmp_path / "kb.json"
    kb_file.write_text(_json.dumps({"entries": [{"ts": 1, "insight": "shared"}]}))

    load_kb(str(kb_file))
    migrate_json_history(tmp_path, kb_path=kb_file, state_path=tmp_path / "none.json",
                         history_path=tmp_path / "none2.json")

    from ouroboros.storage import OuroborosStorage
    assert OuroborosStorage().knowledge_count() == 1


# -- failure injection: no cutover without a durable rollback artifact -------

def test_no_comment_cutover_without_a_snapshot(tmp_path, monkeypatch):
    """Stripping the list from state.json with no snapshot would erase it."""
    import json as _json

    from ouroboros import moltbook, state_migration

    state_file = tmp_path / "state.json"
    state_file.write_text(_json.dumps({
        "comment_history": [{"ts": 1.0, "post_id": "p1", "comment": "legacy"}],
    }))
    monkeypatch.setattr(moltbook, "_state_path", lambda: str(state_file))
    monkeypatch.setattr(state_migration, "freeze_rollback_snapshot", lambda p: None)

    state = moltbook.load_state()
    moltbook.save_state(state)

    # The legacy list is still authoritative in the file.
    assert _json.loads(state_file.read_text())["comment_history"][0]["comment"] == "legacy"


def test_no_comment_cutover_when_a_record_fails_to_import(tmp_path, monkeypatch):
    import json as _json

    from ouroboros import moltbook

    state_file = tmp_path / "state.json"
    state_file.write_text(_json.dumps({
        "comment_history": [
            {"ts": 1.0, "post_id": "good", "comment": "a"},
            {"ts": 2.0, "post_id": "bad", "comment": "b"},
        ],
    }))
    monkeypatch.setattr(moltbook, "_state_path", lambda: str(state_file))

    real = moltbook._comment_storage()

    class Flaky:
        def comment_count(self):
            return real.comment_count()

        def get_comment_history(self, limit=None):
            return real.get_comment_history(limit)

        def append_comment(self, entry):
            if entry.get("post_id") == "bad":
                raise OSError("disk full")
            return real.append_comment(entry)

    monkeypatch.setattr(moltbook, "_comment_storage", lambda: Flaky())

    state = moltbook.load_state()
    moltbook.save_state(state)

    assert len(_json.loads(state_file.read_text())["comment_history"]) == 2


def test_no_knowledge_cutover_without_a_snapshot(tmp_path, monkeypatch):
    import json as _json

    from ouroboros import state_migration
    from ouroboros.knowledge_base import load_kb, save_kb

    kb_file = tmp_path / "kb.json"
    kb_file.write_text(_json.dumps({"entries": [{"ts": 1, "insight": "legacy"}]}))
    monkeypatch.setattr(state_migration, "freeze_rollback_snapshot", lambda p: None)

    kb = load_kb(str(kb_file))

    assert [e["insight"] for e in kb["entries"]] == ["legacy"]
    assert _json.loads(kb_file.read_text())["entries"], "file still authoritative"


def test_a_partial_snapshot_is_not_accepted(tmp_path):
    from ouroboros.state_migration import freeze_rollback_snapshot

    source = tmp_path / "state.json"
    source.write_text('{"real": true}')
    (tmp_path / "state.json.pre-sqlite").write_text("")  # truncated

    snapshot = freeze_rollback_snapshot(source)

    assert snapshot is not None
    assert snapshot.read_text() == '{"real": true}'


def test_a_failed_comment_write_is_durable_immediately(tmp_path, monkeypatch):
    """A crash before the cycle-end save must not lose an already-public
    comment."""
    import json as _json

    from ouroboros import moltbook

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(moltbook, "_state_path", lambda: str(state_file))
    state = moltbook.load_state()

    class Broken:
        def append_comment(self, entry):
            raise OSError("disk full")

    monkeypatch.setattr(moltbook, "_comment_storage", lambda: Broken())
    moltbook.record_comment(state, {"ts": 1.0, "post_id": "p1", "comment": "posted"})

    # No save_state call here -- the process "crashes" right after.
    on_disk = _json.loads(state_file.read_text())
    assert on_disk["comment_outbox"][0]["comment"] == "posted"


def test_a_knowledge_batch_is_all_or_nothing(tmp_path):
    """Per-entry commits leave half a batch written after a mid-batch failure,
    and the caller has already marked those posts seen."""
    from ouroboros.storage import OuroborosStorage

    store = OuroborosStorage(db_path=tmp_path / "kb.db")
    pairs = [({"insight": f"i{i}"}, f"fp{i}") for i in range(3)]
    assert store.append_knowledge_batch(pairs) == 3
    assert store.knowledge_count() == 3

    # Re-inserting the same batch adds nothing.
    assert store.append_knowledge_batch(pairs) == 0
    assert store.knowledge_count() == 3


def test_a_partial_import_is_not_treated_as_complete_after_restart(tmp_path, monkeypatch):
    """Inferring completion from a non-empty table loses the rest.

    One successful insert makes count() non-zero, so a count-based guard would
    skip the remaining legacy records on the next start and then strip the
    file that still held them.
    """
    import json as _json

    from ouroboros import moltbook

    state_file = tmp_path / "state.json"
    state_file.write_text(_json.dumps({
        "comment_history": [
            {"ts": 1.0, "post_id": "good", "comment": "a"},
            {"ts": 2.0, "post_id": "bad", "comment": "b"},
        ],
    }))
    monkeypatch.setattr(moltbook, "_state_path", lambda: str(state_file))

    real = moltbook._comment_storage()
    fail_on_bad = {"active": True}

    class Flaky:
        def __getattr__(self, name):
            return getattr(real, name)

        def append_comment(self, entry):
            if fail_on_bad["active"] and entry.get("post_id") == "bad":
                raise OSError("disk full")
            return real.append_comment(entry)

    monkeypatch.setattr(moltbook, "_comment_storage", lambda: Flaky())

    # First start: "good" imports, "bad" fails, cutover abandoned.
    moltbook.save_state(moltbook.load_state())
    assert len(_json.loads(state_file.read_text())["comment_history"]) == 2

    # Second start, still failing: the table is non-empty now, but the file
    # must still be authoritative.
    moltbook.save_state(moltbook.load_state())
    assert len(_json.loads(state_file.read_text())["comment_history"]) == 2

    # Third start, disk recovered: the import completes and only now is the
    # file stripped.
    fail_on_bad["active"] = False
    state = moltbook.load_state()
    moltbook.save_state(state)
    assert "comment_history" not in _json.loads(state_file.read_text())
    assert len(state["comment_history"]) == 2, "both records survived"


def test_a_completed_migration_is_not_repeated(tmp_path, monkeypatch):
    """Once marked done, a stale legacy list must not be re-imported."""
    import json as _json

    from ouroboros import moltbook

    state_file = tmp_path / "state.json"
    state_file.write_text(_json.dumps({
        "comment_history": [{"ts": 1.0, "post_id": "p1", "comment": "legacy"}],
    }))
    monkeypatch.setattr(moltbook, "_state_path", lambda: str(state_file))

    moltbook.save_state(moltbook.load_state())
    # Someone restores an old state.json over the top.
    state_file.write_text(_json.dumps({
        "comment_history": [{"ts": 1.0, "post_id": "p1", "comment": "legacy"}],
    }))
    state = moltbook.load_state()

    assert len(state["comment_history"]) == 1


def test_a_failed_knowledge_batch_is_queued_not_lost(tmp_path, monkeypatch):
    """The source posts are already in seen_post_ids, so a lost batch is never
    regenerated -- later cycles skip those posts."""
    import json as _json

    from ouroboros import moltbook

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(moltbook, "_state_path", lambda: str(state_file))
    state = moltbook.load_state()

    monkeypatch.setattr(
        "ouroboros.knowledge_base.add_entries",
        lambda entries, path=None: (_ for _ in ()).throw(OSError("disk full")),
    )
    moltbook._record_knowledge(state, [{"insight": "would be lost", "ts": 1}])

    on_disk = _json.loads(state_file.read_text())
    assert on_disk["knowledge_outbox"][0]["insight"] == "would be lost"


def test_a_queued_knowledge_batch_is_retried(tmp_path, monkeypatch):
    import json as _json

    from ouroboros import moltbook
    from ouroboros.knowledge_base import load_kb

    state_file = tmp_path / "state.json"
    state_file.write_text(_json.dumps({
        "knowledge_outbox": [{"insight": "recovered", "ts": 1}],
    }))
    monkeypatch.setattr(moltbook, "_state_path", lambda: str(state_file))

    state = moltbook.load_state()

    assert state["knowledge_outbox"] == []
    assert [e["insight"] for e in load_kb(str(tmp_path / "kb.json"))["entries"]] == [
        "recovered",
    ]


def test_partial_improvement_import_is_retried_after_restart(tmp_path, monkeypatch):
    """Same inference bug as the other two collections: one successful insert
    would make a count-based check treat the import as finished."""
    import json as _json

    from ouroboros import evaluation as ev

    hist = tmp_path / "config" / "improvement_history.json"
    hist.parent.mkdir(parents=True)
    hist.write_text(_json.dumps([
        {"task_id": "good", "timestamp": 1.0, "outcome": "success",
         "task_type": "fix_bug", "description": "d", "test_delta": {},
         "pr_url": "", "feedback": ""},
        {"task_id": "bad", "timestamp": 2.0, "outcome": "success",
         "task_type": "fix_bug", "description": "d", "test_delta": {},
         "pr_url": "", "feedback": ""},
    ]))

    real = ev._history_storage(tmp_path)
    failing = {"active": True}

    class Flaky:
        def __getattr__(self, name):
            return getattr(real, name)

        def append_improvement(self, record):
            if failing["active"] and record.get("task_id") == "bad":
                raise OSError("locked")
            return real.append_improvement(record)

    monkeypatch.setattr(ev, "_history_storage", lambda root=None: Flaky())

    assert len(ev.load_history(tmp_path)) == 1        # partial
    assert not real.migration_done("improvement_history_v1")

    failing["active"] = False
    assert len(ev.load_history(tmp_path)) == 2        # retried and completed
    assert real.migration_done("improvement_history_v1")


def test_a_failed_improvement_write_falls_back_to_json(tmp_path, monkeypatch):
    """The PR already exists; the record must not be lost."""
    import json as _json

    from ouroboros import evaluation as ev
    from ouroboros.improvement import ImprovementResult, ImprovementTask

    hist = tmp_path / "config" / "improvement_history.json"
    hist.parent.mkdir(parents=True)
    hist.write_text("[]")

    real = ev._history_storage(tmp_path)

    class Broken:
        def __getattr__(self, name):
            return getattr(real, name)

        def append_improvement(self, record):
            raise OSError("disk full")

    monkeypatch.setattr(ev, "_history_storage", lambda root=None: Broken())

    task = ImprovementTask("t1", "fix_bug", "d", [], "e")
    ev.record_improvement(
        ImprovementResult(task=task, status="success", pr_url="https://x/1"),
        tmp_path,
    )

    persisted = _json.loads(hist.read_text())
    assert [r["task_id"] for r in persisted] == ["t1"]
