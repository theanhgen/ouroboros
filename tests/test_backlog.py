import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from ouroboros.backlog import (
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
