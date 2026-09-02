import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from ouroboros.knowledge_base import load_kb, save_kb, add_entries, get_summary

class TestKnowledgeBase:
    def setup_method(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.kb_file = self.tmp_dir / "knowledge_base.json"

    def teardown_method(self):
        shutil.rmtree(self.tmp_dir)

    def test_load_kb_empty(self):
        kb = load_kb(str(self.kb_file))
        assert kb["entries"] == []
        assert kb["summary_updated_at"] == 0

    def test_save_load_kb(self):
        # save_kb persists the summary cache; entries are owned by SQLite.
        add_entries([{"ts": 100, "insight": "test"}], str(self.kb_file))
        save_kb(
            {"summary_cache": "test summary", "summary_updated_at": 100},
            str(self.kb_file),
        )

        loaded = load_kb(str(self.kb_file))
        assert loaded["entries"][0]["insight"] == "test"
        assert loaded["summary_cache"] == "test summary"

    def test_legacy_json_entries_are_imported_once(self):
        """An existing deployment must not need a manual step."""
        import json as _json

        self.kb_file.write_text(_json.dumps({
            "entries": [{"ts": 1, "insight": "from json"}],
            "summary_cache": "",
            "summary_updated_at": 0,
        }))

        first = load_kb(str(self.kb_file))
        second = load_kb(str(self.kb_file))

        assert [e["insight"] for e in first["entries"]] == ["from json"]
        assert [e["insight"] for e in second["entries"]] == ["from json"]

    def test_add_entries_keeps_everything(self):
        """No cap: the 200-entry limit existed only because each append
        rewrote the whole JSON list."""
        add_entries([{"ts": i, "insight": f"i{i}"} for i in range(150)], str(self.kb_file))
        assert len(load_kb(str(self.kb_file))["entries"]) == 150

        add_entries(
            [{"ts": i + 150, "insight": f"i{i+150}"} for i in range(100)],
            str(self.kb_file),
        )
        kb = load_kb(str(self.kb_file))
        assert len(kb["entries"]) == 250
        assert kb["entries"][0]["insight"] == "i0", "nothing is dropped"

    @patch("ouroboros.llm.generate_kb_summary")
    def test_get_summary_refresh(self, mock_gen):
        mock_gen.return_value = "newly generated summary"
        kb = {
            "entries": [{"ts": 100, "insight": "i1"}],
            "summary_cache": "old summary",
            "summary_updated_at": 50 # very old
        }
        # Force refresh via age
        summary = get_summary(MagicMock(), kb, path=str(self.kb_file))
        assert summary == "newly generated summary"
        assert mock_gen.called

    @patch("ouroboros.llm.generate_kb_summary")
    def test_get_summary_cached(self, mock_gen):
        import time
        now = int(time.time())
        kb = {
            "entries": [{"ts": now - 100, "insight": "i1"}],
            "summary_cache": "recent summary",
            "summary_updated_at": now - 50 # recent
        }
        summary = get_summary(MagicMock(), kb, path=str(self.kb_file))
        assert summary == "recent summary"
        assert not mock_gen.called

    @patch("ouroboros.llm.generate_kb_summary")
    def test_get_summary_with_null_updated_at(self, mock_gen):
        """A JSON null timestamp must degrade to a refresh, not a TypeError."""
        mock_gen.return_value = "newly generated summary"
        kb = {
            "entries": [{"ts": 100, "insight": "i1"}],
            "summary_cache": "old summary",
            "summary_updated_at": None,  # what a JSON null deserializes to
        }
        summary = get_summary(MagicMock(), kb, path=str(self.kb_file))
        assert summary == "newly generated summary"
        assert mock_gen.called
