import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from ouroboros.metrics import (
    load_metrics,
    save_metrics,
    record_snapshot,
    get_summary,
)

class TestMetrics:
    def setup_method(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.config_dir = self.tmp_dir / "config"
        self.config_dir.mkdir()
        self.src_dir = self.tmp_dir / "src"
        self.src_dir.mkdir()
        self.test_dir = self.tmp_dir / "tests"
        self.test_dir.mkdir()

    def teardown_method(self):
        shutil.rmtree(self.tmp_dir)

    def test_load_save_metrics(self):
        snapshots = [{"timestamp": 123, "src_lines": 100}]
        save_metrics(self.tmp_dir, snapshots)
        
        loaded = load_metrics(self.tmp_dir)
        assert len(loaded) == 1
        assert loaded[0]["src_lines"] == 100

    def test_save_metrics_bounded(self):
        # Create 205 snapshots
        snapshots = [{"timestamp": i, "src_lines": i} for i in range(205)]
        save_metrics(self.tmp_dir, snapshots)
        
        loaded = load_metrics(self.tmp_dir)
        assert len(loaded) == 200
        assert loaded[0]["timestamp"] == 5  # Should have dropped the first 5

    def test_load_metrics_malformed_file_returns_list(self):
        # A corrupt metrics.json must never leak a non-list to callers
        path = self.config_dir / "metrics.json"
        for raw in ("null", "42", '"snapshots"', '{"snapshots": null}',
                    '{"snapshots": 5}', "{}"):
            path.write_text(raw, encoding="utf-8")
            loaded = load_metrics(self.tmp_dir)
            assert loaded == [], f"{raw!r} produced {loaded!r}"

    @patch("ouroboros.evaluation.load_history")
    def test_record_snapshot(self, mock_load_history):
        # Setup mock history
        mock_history = [
            MagicMock(timestamp=time.time() - 100, outcome="merged"),
            MagicMock(timestamp=time.time() - 200, outcome="failed"),
        ]
        mock_load_history.return_value = mock_history
        
        # Setup some dummy source files
        (self.src_dir / "main.py").write_text("line1\nline2\nline3")
        (self.test_dir / "test_main.py").write_text("test1\ntest2")
        
        snapshot = record_snapshot(self.tmp_dir)
        
        assert snapshot["src_lines"] == 3
        assert snapshot["test_lines"] == 2
        assert snapshot["total_improvements"] == 2
        assert snapshot["recent_attempts_30d"] == 2
        assert snapshot["recent_successes_30d"] == 1
        assert snapshot["success_rate_30d"] == 50.0

    @patch("ouroboros.evaluation.load_history")
    def test_record_snapshot_includes_policy_decisions(self, mock_load_history):
        mock_load_history.return_value = []
        result = SimpleNamespace(
            task=SimpleNamespace(task_type="add_feature"),
            status="failed",
            test_after=None,
            changes=[
                SimpleNamespace(
                    file_path="README.md",
                    original_content="",
                    new_content="line1\nline2\n",
                )
            ],
        )

        snapshot = record_snapshot(self.tmp_dir, result)

        assert snapshot["policy_scope"]["file_paths"] == ["README.md"]
        assert snapshot["policy_scope"]["is_valid"] is False
        assert "Out of scope" in snapshot["policy_scope"]["violations"][0]
        assert snapshot["policy_size"]["num_files"] == 1
        assert snapshot["policy_size"]["num_lines"] == 2
        assert snapshot["policy_size"]["max_lines"] == 200

    @patch("ouroboros.evaluation.load_history")
    def test_record_snapshot_appends_to_existing_history(self, mock_load_history):
        mock_load_history.return_value = []
        save_metrics(self.tmp_dir, [{"timestamp": 1, "src_lines": 7}])

        record_snapshot(self.tmp_dir)

        loaded = load_metrics(self.tmp_dir)
        assert len(loaded) == 2
        assert loaded[0]["src_lines"] == 7

    @patch("ouroboros.evaluation.load_history")
    def test_record_snapshot_appends_to_a_bare_list_file(self, mock_load_history):
        # The original on-disk shape. Readable, so it must not be quarantined.
        mock_load_history.return_value = []
        path = self.config_dir / "metrics.json"
        path.write_text(json.dumps([{"timestamp": 1, "src_lines": 7}]), "utf-8")

        record_snapshot(self.tmp_dir)

        assert len(load_metrics(self.tmp_dir)) == 2
        assert list(self.config_dir.glob("metrics.json.corrupt-*")) == []

    @patch("ouroboros.evaluation.load_history")
    def test_record_snapshot_quarantines_an_unreadable_file(self, mock_load_history):
        # The regression: load_metrics returns [] for a file it cannot parse,
        # and appending to that [] used to overwrite the history wholesale --
        # then config/metrics.json is auto-committed, so the loss is pushed.
        mock_load_history.return_value = []
        path = self.config_dir / "metrics.json"
        damaged = '{"snapshots": [{"timestamp": 1, "src_l'
        path.write_text(damaged, encoding="utf-8")

        record_snapshot(self.tmp_dir)

        quarantined = list(self.config_dir.glob("metrics.json.corrupt-*"))
        assert len(quarantined) == 1, "the damaged history was not kept"
        assert quarantined[0].read_text(encoding="utf-8") == damaged
        assert len(load_metrics(self.tmp_dir)) == 1

    @patch("ouroboros.evaluation.load_history")
    def test_record_snapshot_quarantines_a_wrong_shaped_file(self, mock_load_history):
        # Parses, but carries no snapshot list: still not ours to overwrite.
        mock_load_history.return_value = []
        path = self.config_dir / "metrics.json"
        damaged = '{"snapshots": {"1": {"src_lines": 7}}}'
        path.write_text(damaged, encoding="utf-8")

        record_snapshot(self.tmp_dir)

        quarantined = list(self.config_dir.glob("metrics.json.corrupt-*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text(encoding="utf-8") == damaged
        assert len(load_metrics(self.tmp_dir)) == 1

    @patch("ouroboros.evaluation.load_history")
    def test_two_quarantines_in_one_second_do_not_collide(self, mock_load_history):
        mock_load_history.return_value = []
        path = self.config_dir / "metrics.json"

        for raw in ("first-damaged", "second-damaged"):
            path.write_text(raw, encoding="utf-8")
            record_snapshot(self.tmp_dir)

        kept = sorted(
            p.read_text(encoding="utf-8")
            for p in self.config_dir.glob("metrics.json.corrupt-*")
        )
        assert kept == ["first-damaged", "second-damaged"]

    @patch("ouroboros.evaluation.load_history")
    def test_record_snapshot_keeps_the_history_bounded(self, mock_load_history):
        mock_load_history.return_value = []
        save_metrics(self.tmp_dir, [{"timestamp": i} for i in range(200)])

        record_snapshot(self.tmp_dir)

        loaded = load_metrics(self.tmp_dir)
        assert len(loaded) == 200
        assert loaded[0]["timestamp"] == 1  # the oldest was dropped

    @patch("ouroboros.evaluation.load_history")
    def test_concurrent_snapshots_do_not_lose_records(self, mock_load_history):
        """load-append-save as three steps drops whichever write landed first."""
        import threading

        mock_load_history.return_value = []
        errors = []

        def writer():
            try:
                for _ in range(5):
                    record_snapshot(self.tmp_dir)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(load_metrics(self.tmp_dir)) == 20

    def test_get_summary_empty(self):
        summary = get_summary(self.tmp_dir)
        assert summary == "No metrics recorded yet."

    def test_get_summary(self):
        snapshots = [
            {
                "timestamp": time.time() - 10 * 86400, # 10 days ago
                "src_lines": 100,
                "test_lines": 50,
                "total_improvements": 10,
                "recent_attempts_30d": 10,
                "recent_successes_30d": 5,
                "success_rate_30d": 50.0,
            },
            {
                "timestamp": time.time(),
                "src_lines": 110,
                "test_lines": 60,
                "total_improvements": 12,
                "recent_attempts_30d": 12,
                "recent_successes_30d": 7,
                "success_rate_30d": 58.3,
            }
        ]
        save_metrics(self.tmp_dir, snapshots)
        
        summary = get_summary(self.tmp_dir)
        assert "Source: 110 LOC" in summary
        assert "Tests: 60 LOC" in summary
        assert "Success rate (30d): 58.3%" in summary
        assert "7d trend: src +10 LOC, tests +10 LOC, rate +8.3%" in summary
