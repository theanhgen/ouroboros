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
