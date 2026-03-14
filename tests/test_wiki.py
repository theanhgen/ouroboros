import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from ouroboros.wiki import (
    generate_architecture_page,
    generate_metrics_page,
    generate_changelog_page,
    generate_config_page,
    generate_failures_page,
    update_wiki,
)

class TestWiki:
    def setup_method(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.wiki_dir = self.tmp_dir / "docs" / "wiki"
        # wiki_dir created by _wiki_path in the code

    def teardown_method(self):
        shutil.rmtree(self.tmp_dir)

    @patch("ouroboros.codebase.get_function_signatures")
    def test_generate_architecture_page(self, mock_sigs):
        # Setup dummy source files
        src_dir = self.tmp_dir / "src" / "ouroboros"
        src_dir.mkdir(parents=True)
        (src_dir / "module1.py").write_text("def func1(): pass\ndef func2(a, b): pass")
        
        mock_sigs.return_value = ["func1()", "func2(a, b)"]
        
        content = generate_architecture_page(self.tmp_dir)
        assert "# Architecture" in content
        assert "### `module1.py` (2 lines)" in content
        assert "- `func1()`" in content
        assert "- `func2(a, b)`" in content

    @patch("ouroboros.evaluation.load_history")
    @patch("ouroboros.metrics.load_metrics")
    def test_generate_metrics_page(self, mock_metrics, mock_history):
        mock_history.return_value = [
            MagicMock(outcome="merged", task_type="feat"),
            MagicMock(outcome="failed", task_type="fix"),
        ]
        mock_metrics.return_value = [
            {"timestamp": time.time(), "src_lines": 100, "test_lines": 50, "success_rate_30d": 50.0}
        ]
        
        content = generate_metrics_page(self.tmp_dir)
        assert "# Metrics" in content
        assert "- Total improvement attempts: 2" in content
        assert "- Merged: 1" in content
        assert "| feat | 1 | 1 | 0 | 100% |" in content
        assert "- Source lines: 100" in content

    @patch("ouroboros.evaluation.load_history")
    def test_generate_changelog_page(self, mock_history):
        mock_history.return_value = [
            MagicMock(
                timestamp=time.time(),
                outcome="merged",
                task_type="refactor",
                description="clean code",
                pr_url="http://pr/1",
                test_delta={"before": {"passed": 10}, "after": {"passed": 11}}
            )
        ]
        
        content = generate_changelog_page(self.tmp_dir)
        assert "# Changelog" in content
        assert "[MERGED] **refactor**: clean code" in content
        assert "([PR](http://pr/1))" in content
        assert "Tests: 10p/0f -> 11p/0f" in content

    def test_generate_config_page(self):
        # This tests SafetyConfig and RunnerConfig generation
        content = generate_config_page(self.tmp_dir)
        assert "# Configuration Reference" in content
        assert "## Safety Config" in content
        assert "## Runner Config" in content
        assert "`pr_only`" in content
        assert "`interval_seconds`" in content

    @patch("ouroboros.evaluation.load_history")
    def test_generate_failures_page(self, mock_history):
        mock_history.return_value = [
            MagicMock(
                outcome="failed",
                task_type="fix_bug",
                description="crash on startup",
                details="NameError: name 'x' is not defined",
                feedback="Check global scope"
            )
        ]
        
        content = generate_failures_page(self.tmp_dir)
        assert "# Failure Patterns" in content
        assert "## fix_bug (1 failures)" in content
        assert "crash on startup" in content
        assert "Reason: NameError" in content
        assert "Feedback: Check global scope" in content

    @patch("ouroboros.wiki.generate_architecture_page")
    @patch("ouroboros.wiki.generate_metrics_page")
    @patch("ouroboros.wiki.generate_changelog_page")
    @patch("ouroboros.wiki.generate_config_page")
    @patch("ouroboros.wiki.generate_failures_page")
    def test_update_wiki(self, m_fail, m_conf, m_chan, m_metr, m_arch):
        m_arch.return_value = "arch"
        m_metr.return_value = "metr"
        m_chan.return_value = "chan"
        m_conf.return_value = "conf"
        m_fail.return_value = "fail"
        
        updated = update_wiki(self.tmp_dir)
        
        assert len(updated) == 6 # 5 pages + index
        assert (self.wiki_dir / "architecture.md").read_text() == "arch"
        assert (self.wiki_dir / "index.md").exists()
        assert "docs/wiki/architecture.md" in updated
