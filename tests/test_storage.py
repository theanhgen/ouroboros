"""Tests for the JSON file helpers and the SQLite cycle/metrics store."""

import json
import os
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from ouroboros.storage import (
    CycleRecord,
    MetricRecord,
    OuroborosStorage,
    load_json_file,
    save_json_file,
)


# -- load_json_file ----------------------------------------------------------

def test_load_json_file_reads_a_file(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"a": 1}))
    assert load_json_file(path) == {"a": 1}


def test_load_json_file_accepts_a_str_path(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("[1, 2]")
    assert load_json_file(str(path)) == [1, 2]


def test_load_json_file_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # expanduser prefers this on Windows
    (tmp_path / "x.json").write_text('{"home": true}')
    assert load_json_file("~/x.json") == {"home": True}


def test_load_json_file_missing_without_default_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_json_file(tmp_path / "nope.json")


def test_load_json_file_missing_with_default(tmp_path):
    assert load_json_file(tmp_path / "nope.json", default={"d": 1}) == {"d": 1}


def test_load_json_file_corrupt_without_default_raises(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("{ not json")
    with pytest.raises(json.JSONDecodeError):
        load_json_file(path)


def test_load_json_file_corrupt_with_default_logs(tmp_path, caplog):
    path = tmp_path / "x.json"
    path.write_text("{ not json")

    with caplog.at_level("WARNING"):
        assert load_json_file(path, default=[], error_msg="bad file") == []

    assert "bad file" in caplog.text


def test_load_json_file_corrupt_without_error_msg_is_quiet(tmp_path, caplog):
    path = tmp_path / "x.json"
    path.write_text("{ not json")

    with caplog.at_level("WARNING"):
        assert load_json_file(path, default=[]) == []

    assert caplog.text == ""


def test_load_json_file_uses_the_supplied_logger(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("{ not json")
    logger = mock.MagicMock()

    load_json_file(path, default=[], error_msg="oops", logger=logger)

    logger.warning.assert_called_once_with("oops")


def test_load_json_file_binary_garbage_is_corruption(tmp_path):
    """A crash-zeroed file never reaches the JSON parser."""
    path = tmp_path / "x.json"
    path.write_bytes(b"\x00\x81\xfe" * 50)
    assert load_json_file(path, default={"d": 1}) == {"d": 1}


def test_load_json_file_propagates_read_errors(tmp_path):
    """Unreadable is not missing: a default here would be overwritten back."""
    path = tmp_path / "x.json"
    path.write_text("{}")

    real_open = Path.open

    def unreadable(self, *a, **kw):
        if str(self) == str(path):
            raise PermissionError("EACCES")
        return real_open(self, *a, **kw)

    with mock.patch.object(Path, "open", unreadable):
        with pytest.raises(PermissionError):
            load_json_file(path, default={"d": 1})


def test_load_json_file_default_is_deep_copied(tmp_path):
    """Callers must not be able to mutate each other's default."""
    default = {"items": []}

    first = load_json_file(tmp_path / "a.json", default=default)
    first["items"].append("x")
    second = load_json_file(tmp_path / "b.json", default=default)

    assert second == {"items": []}
    assert default == {"items": []}


# -- save_json_file ----------------------------------------------------------

def test_save_json_file_round_trip(tmp_path):
    path = tmp_path / "x.json"
    save_json_file(path, {"a": [1, 2], "b": "c"})
    assert load_json_file(path) == {"a": [1, 2], "b": "c"}


def test_save_json_file_creates_parent_directories(tmp_path):
    path = tmp_path / "deep" / "nested" / "x.json"
    save_json_file(path, {"a": 1})
    assert path.exists()


def test_save_json_file_overwrites(tmp_path):
    path = tmp_path / "x.json"
    save_json_file(path, {"first": True})
    save_json_file(path, {"second": True})
    assert load_json_file(path) == {"second": True}


def test_save_json_file_leaves_no_temp_file(tmp_path):
    path = tmp_path / "x.json"
    save_json_file(path, {"a": 1})
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_json_file_is_atomic(tmp_path):
    """A failure during the write must leave the previous contents intact."""
    path = tmp_path / "x.json"
    save_json_file(path, {"original": True})

    with mock.patch("ouroboros.storage.os.replace", side_effect=OSError("boom")):
        with pytest.raises(OSError):
            save_json_file(path, {"replacement": True})

    assert load_json_file(path) == {"original": True}
    assert list(tmp_path.glob("*.tmp")) == [], "a failed write must not leave scratch files"


def test_save_json_file_sort_keys(tmp_path):
    path = tmp_path / "x.json"
    save_json_file(path, {"b": 1, "a": 2}, sort_keys=True)
    assert list(json.loads(path.read_text())) == ["a", "b"]


def test_save_json_file_indent(tmp_path):
    path = tmp_path / "x.json"
    save_json_file(path, {"a": 1}, indent=0)
    assert "\n" in path.read_text()


def test_save_json_file_rejects_unserialisable_data(tmp_path):
    path = tmp_path / "x.json"
    with pytest.raises(TypeError):
        save_json_file(path, {"bad": object()})
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_json_file_uses_a_unique_temp_name(tmp_path):
    """A shared "<name>.tmp" lets concurrent writers truncate each other."""
    path = tmp_path / "x.json"
    seen = []

    real_replace = os.replace

    def capture(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    with mock.patch("ouroboros.storage.os.replace", side_effect=capture):
        save_json_file(path, {"a": 1})
        save_json_file(path, {"a": 2})

    assert len(set(seen)) == 2, f"temp names must differ, got {seen}"


def test_concurrent_writers_do_not_corrupt_the_target(tmp_path):
    """Whatever wins, the file must be complete and parseable."""
    import threading

    path = tmp_path / "x.json"
    errors = []

    def writer(n):
        try:
            for _ in range(20):
                save_json_file(path, {"writer": n, "payload": [n] * 200})
        except Exception as exc:  # pragma: no cover - failure surfaces below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    data = load_json_file(path)
    assert data["payload"] == [data["writer"]] * 200
    assert list(tmp_path.glob("*.tmp")) == []


# -- OuroborosStorage --------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return OuroborosStorage(db_path=tmp_path / "ouroboros.db")


def test_storage_creates_the_database(tmp_path):
    db = tmp_path / "nested" / "ouroboros.db"
    OuroborosStorage(db_path=db)
    assert db.exists()


def test_storage_init_is_idempotent(tmp_path):
    db = tmp_path / "ouroboros.db"
    OuroborosStorage(db_path=db).record_cycle(CycleRecord(task_type="fix_bug"))
    reopened = OuroborosStorage(db_path=db)
    assert len(reopened.get_recent_cycles()) == 1


def test_record_cycle_returns_an_id(store):
    cycle_id = store.record_cycle(
        CycleRecord(ts=1000.0, task_type="fix_bug", model="m", status="success",
                    description="d")
    )
    assert isinstance(cycle_id, int)
    assert cycle_id > 0


def test_record_cycle_persists_every_field(store):
    """Asserting only the id would let model/status/description regress."""
    cycle_id = store.record_cycle(
        CycleRecord(
            ts=1000.0,
            task_type="fix_bug",
            model="gpt-test",
            status="success",
            description="a description",
        )
    )

    row = store.get_recent_cycles()[0]
    assert row["id"] == cycle_id
    assert row["ts"] == 1000.0
    assert row["task_type"] == "fix_bug"
    assert row["model"] == "gpt-test"
    assert row["status"] == "success"
    assert row["description"] == "a description"


def test_record_cycle_ids_increment(store):
    first = store.record_cycle(CycleRecord(task_type="a"))
    second = store.record_cycle(CycleRecord(task_type="b"))
    assert second > first


def test_record_cycle_defaults_ts_to_now(store):
    import time

    before = time.time()
    store.record_cycle(CycleRecord(task_type="fix_bug"))
    row = store.get_recent_cycles()[0]
    assert row["ts"] >= before


def test_get_recent_cycles_is_newest_first(store):
    store.record_cycle(CycleRecord(ts=1000.0, task_type="old"))
    store.record_cycle(CycleRecord(ts=3000.0, task_type="new"))
    store.record_cycle(CycleRecord(ts=2000.0, task_type="middle"))

    assert [c["task_type"] for c in store.get_recent_cycles()] == [
        "new", "middle", "old",
    ]


def test_get_recent_cycles_respects_the_limit(store):
    for i in range(5):
        store.record_cycle(CycleRecord(ts=float(i), task_type=f"t{i}"))
    assert len(store.get_recent_cycles(limit=2)) == 2


def test_get_recent_cycles_empty(store):
    assert store.get_recent_cycles() == []


def test_get_recent_cycles_joins_metrics(store):
    cycle_id = store.record_cycle(CycleRecord(ts=1.0, task_type="fix_bug"))
    store.record_metrics(
        MetricRecord(cycle_id=cycle_id, tokens_in=10, tokens_out=20, cost=0.5)
    )

    row = store.get_recent_cycles()[0]
    assert (row["tokens_in"], row["tokens_out"], row["cost"]) == (10, 20, 0.5)


def test_get_recent_cycles_without_metrics_has_nulls(store):
    """The join is a LEFT JOIN -- a cycle with no metrics still appears."""
    store.record_cycle(CycleRecord(ts=1.0, task_type="fix_bug"))
    row = store.get_recent_cycles()[0]
    assert row["task_type"] == "fix_bug"
    assert row["cost"] is None


def test_record_metrics_replaces_on_conflict(store):
    cycle_id = store.record_cycle(CycleRecord(task_type="fix_bug"))
    store.record_metrics(MetricRecord(cycle_id=cycle_id, cost=1.0))
    store.record_metrics(MetricRecord(cycle_id=cycle_id, cost=2.0))

    assert store.get_total_cost() == 2.0


def test_record_metrics_rejects_an_orphan_cycle(store):
    """Without PRAGMA foreign_keys the REFERENCES clause is decorative, and an
    orphan metric is counted by get_total_cost while invisible in cycles."""
    with pytest.raises(sqlite3.IntegrityError):
        store.record_metrics(MetricRecord(cycle_id=99999, cost=5.0))

    assert store.get_total_cost() == 0.0


def test_get_total_cost_empty_is_zero(store):
    """SUM over no rows is NULL, which must not leak out as None."""
    assert store.get_total_cost() == 0.0


def test_get_total_cost_sums(store):
    for cost in (0.25, 0.5, 1.0):
        cycle_id = store.record_cycle(CycleRecord(task_type="t"))
        store.record_metrics(MetricRecord(cycle_id=cycle_id, cost=cost))

    assert store.get_total_cost() == pytest.approx(1.75)


def test_add_and_search_embeddings(store):
    store.add_embedding("code", "src/a.py", "contents", [0.1, 0.2])

    rows = store.search_embeddings()
    assert len(rows) == 1
    assert rows[0]["content_type"] == "code"
    assert rows[0]["ref_id"] == "src/a.py"
    assert rows[0]["content"] == "contents"
    assert json.loads(rows[0]["embedding"]) == [0.1, 0.2]
    assert rows[0]["ts"] > 0


def test_search_embeddings_filters_by_content_type(store):
    store.add_embedding("code", "a", "x", [0.1])
    store.add_embedding("failure", "b", "y", [0.2])

    rows = store.search_embeddings(content_type="failure")
    assert [r["ref_id"] for r in rows] == ["b"]


def test_search_embeddings_respects_the_limit(store):
    for i in range(5):
        store.add_embedding("code", f"f{i}", "x", [float(i)])
    assert len(store.search_embeddings(limit=2)) == 2


def test_search_embeddings_empty(store):
    assert store.search_embeddings() == []


def test_search_embeddings_is_newest_first(store):
    # Distinct timestamps: without them both rows can share a ts and any
    # ordering satisfies the assertion, so an ORDER BY regression would pass.
    with mock.patch("ouroboros.storage.time.time", side_effect=[100.0, 200.0]):
        store.add_embedding("code", "first", "x", [0.1])
        store.add_embedding("code", "second", "y", [0.2])

    assert [r["ref_id"] for r in store.search_embeddings()] == ["second", "first"]


def test_search_embeddings_limit_takes_the_newest(store):
    with mock.patch("ouroboros.storage.time.time", side_effect=[100.0, 200.0, 300.0]):
        for ref in ("oldest", "middle", "newest"):
            store.add_embedding("code", ref, "x", [0.1])

    assert [r["ref_id"] for r in store.search_embeddings(limit=2)] == [
        "newest", "middle",
    ]


def test_embeddings_survive_reopening(tmp_path):
    db = tmp_path / "ouroboros.db"
    OuroborosStorage(db_path=db).add_embedding("code", "a", "x", [0.5])

    rows = OuroborosStorage(db_path=db).search_embeddings()
    assert json.loads(rows[0]["embedding"]) == [0.5]


def test_storage_defaults_to_the_repo_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "ouroboros.storage.get_repo_root", lambda: tmp_path
    )
    store = OuroborosStorage()
    assert store.db_path == tmp_path / "config" / "ouroboros.db"


def test_save_leaves_a_git_repository_clean(tmp_path):
    """Any stray file beside the target wedges the improvement loop.

    commit_auto_state does not stage unknown files and git_ops.is_clean
    counts untracked as dirty, so a leftover lock or temp file would make
    every later cycle abort with a dirty worktree.
    """
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    config = tmp_path / "config"
    config.mkdir()

    save_json_file(config / "backlog.json", {"items": []})
    save_json_file(config / "improvement_history.json", [])

    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    stray = [p for p in status if p not in {"??", "config/backlog.json",
                                            "config/improvement_history.json"}]
    assert stray == [], f"save left files git would flag: {stray}"
