import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from ouroboros.self_improve import (
    _load_prompt_context,
    _parse_prompt_update,
    _build_prompt_update_request,
    run_self_improve,
)

class TestSelfImprove:
    def setup_method(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.src_dir = self.tmp_dir / "src" / "ouroboros"
        self.src_dir.mkdir(parents=True)
        # Mock prompts path to avoid modifying actual prompts
        self.prompts_json = self.src_dir / "prompts.json"
        self.prompts_json.write_text(json.dumps({"comment_system_prompt": "old prompt"}))

    def teardown_method(self):
        shutil.rmtree(self.tmp_dir)

    def test_load_prompt_context(self):
        state = {
            "comment_history": [{"id": i} for i in range(20)],
            "self_question_log": [{"q": i} for i in range(20)],
        }
        context = _load_prompt_context(state)
        assert len(context["recent_comments"]) == 8
        assert len(context["recent_questions"]) == 6
        assert context["recent_comments"][-1]["id"] == 19
        assert context["recent_questions"][-1]["q"] == 19

    def test_parse_prompt_update_valid(self):
        payload = json.dumps({
            "new_prompt": "This is a new valid prompt that is at least sixty characters long.",
            "rationale": "better focus"
        })
        result = _parse_prompt_update(payload)
        assert result is not None
        assert result[0] == "This is a new valid prompt that is at least sixty characters long."
        assert result[1] == "better focus"

    def test_parse_prompt_update_invalid(self):
        # Too short
        assert _parse_prompt_update(json.dumps({"new_prompt": "too short"})) is None
        # Not JSON
        assert _parse_prompt_update("not json") is None
        # Missing key
        assert _parse_prompt_update(json.dumps({"rationale": "missing prompt"})) is None

    def test_build_prompt_update_request(self):
        current = "old prompt"
        context = {"recent_comments": [], "recent_questions": []}
        req = _build_prompt_update_request(current, context)
        assert "old prompt" in req
        assert "JSON" in req

    @patch("ouroboros.self_improve.get_repo_root")
    @patch("ouroboros.self_improve._git_clean")
    @patch("ouroboros.prompts.load_comment_system_prompt")
    @patch("ouroboros.prompts._prompts_path")
    @patch("ouroboros.self_improve._git_commit_and_pr")
    def test_run_self_improve_success(self, mock_pr, mock_path, mock_load, mock_clean, mock_root):
        mock_root.return_value = self.tmp_dir
        mock_clean.return_value = True
        mock_load.return_value = "old prompt"
        mock_path.return_value = str(self.prompts_json)
        mock_pr.return_value = "http://pr/1"
        
        # Mock LLM client
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices[0].message.content = json.dumps({
            "new_prompt": "This is a new valid prompt that is at least sixty characters long and different.",
            "rationale": "improvement"
        })
        mock_client.chat.completions.create.return_value = mock_response
        
        state = {"comment_history": [], "self_question_log": []}
        result = run_self_improve(mock_client, state)
        
        assert "PR created: http://pr/1" in result
        
        # Check if file was updated
        updated_data = json.loads(self.prompts_json.read_text())
        assert updated_data["comment_system_prompt"].startswith("This is a new valid prompt")
        assert updated_data["updated_by"] == "self-improve"

    @patch("ouroboros.self_improve._git_clean")
    def test_run_self_improve_dirty_repo(self, mock_clean):
        mock_clean.return_value = False
        result = run_self_improve(MagicMock(), {})
        assert result is None
