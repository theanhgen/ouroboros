import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from ouroboros.github_improvement import (
    GitHubIssue,
    get_open_issues,
    analyze_issue,
    apply_github_fix,
    IssueResolutionResult,
)

class TestGitHubImprovement:
    def setup_method(self):
        self.repo_root = Path("/tmp/fake_repo") # doesn't need to exist if we mock everything

    @patch("subprocess.run")
    def test_get_open_issues_success(self, mock_run):
        mock_stdout = json.dumps([
            {
                "number": 123,
                "title": "bug report",
                "body": "it crashes",
                "author": {"login": "user1"},
                "url": "http://github/123"
            }
        ])
        mock_run.return_value = MagicMock(stdout=mock_stdout, check_returncode=lambda: None)
        
        issues = get_open_issues(self.repo_root)
        assert len(issues) == 1
        assert issues[0].id == 123
        assert issues[0].author == "user1"

    @patch("subprocess.run")
    def test_get_open_issues_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "gh")
        issues = get_open_issues(self.repo_root)
        assert issues == []

    @patch("ouroboros.llm.chat_completion")
    @patch("ouroboros.github_improvement.list_source_files")
    @patch("ouroboros.github_improvement.get_function_signatures")
    def test_analyze_issue(self, mock_sigs, mock_list, mock_llm):
        mock_list.return_value = [self.repo_root / "src/a.py"]
        mock_sigs.return_value = "def a(): pass"
        mock_llm.return_value = (json.dumps({
            "summary": "fix it",
            "task_type": "bug_fix",
            "target_files": ["src/a.py"],
            "plan": ["fix it"],
            "confidence": 0.9
        }), None)
        
        issue = GitHubIssue(1, "t", "b", "a", "u")
        analysis = analyze_issue(MagicMock(), issue, self.repo_root)
        
        assert analysis["confidence"] == 0.9
        assert "src/a.py" in analysis["target_files"]

    @patch("ouroboros.llm.chat_completion")
    @patch("ouroboros.git_ops.create_branch")
    @patch("ouroboros.git_ops.current_branch")
    @patch("ouroboros.git_ops.checkout_branch")
    @patch("ouroboros.git_ops.commit_changes")
    @patch("ouroboros.git_ops.push_branch")
    @patch("ouroboros.git_ops.create_pr")
    @patch("ouroboros.test_runner.run_tests")
    @patch("ouroboros.github_improvement.read_file_raw")
    @patch("pathlib.Path.write_text")
    @patch("pathlib.Path.exists")
    def test_apply_github_fix_success(self, mock_exists, mock_write, mock_read, mock_test, mock_pr, mock_push, mock_commit, mock_checkout, mock_curr, mock_branch, mock_llm):
        mock_llm.return_value = (json.dumps({
            "explanation": "fixed bug",
            "changes": [{"file_path": "src/a.py", "new_content": "VALUE = 1\n"}]
        }), None)
        mock_exists.return_value = True
        mock_read.return_value = "old code"
        mock_test.return_value = MagicMock(failed=0, errors=0)
        mock_pr.return_value = "http://pr/123"
        mock_curr.return_value = "main"
        
        issue = GitHubIssue(123, "title", "body", "author", "url")
        analysis = {"target_files": ["src/a.py"]}
        
        result = apply_github_fix(MagicMock(), issue, analysis, self.repo_root)
        
        assert result.status == "success"
        assert result.pr_url == "http://pr/123"
        mock_branch.assert_called_once()
        mock_commit.assert_called_once()
        mock_checkout.assert_called_with(self.repo_root, "main")

    @patch("ouroboros.llm.chat_completion")
    @patch("ouroboros.test_runner.run_tests")
    @patch("ouroboros.git_ops.checkout_branch")
    @patch("ouroboros.git_ops.delete_branch")
    @patch("ouroboros.git_ops.current_branch")
    @patch("ouroboros.git_ops.create_branch")
    @patch("pathlib.Path.write_text")
    @patch("pathlib.Path.exists")
    def test_apply_github_fix_test_failure(self, mock_exists, mock_write, mock_branch, mock_curr, mock_del, mock_checkout, mock_test, mock_llm):
        mock_llm.return_value = (json.dumps({
            "explanation": "bad fix",
            "changes": [{"file_path": "src/a.py", "new_content": "VALUE = 2\n"}]
        }), None)
        mock_test.return_value = MagicMock(failed=1, errors=0)
        mock_curr.return_value = "main"
        
        issue = GitHubIssue(123, "title", "body", "author", "url")
        result = apply_github_fix(MagicMock(), issue, {}, self.repo_root)
        
        assert result.status == "failed"
        assert "Tests failed" in result.error
        mock_del.assert_called_once()
        mock_checkout.assert_called_with(self.repo_root, "main")


class TestGitHubFixImportPolicy:
    """This flow writes generated files directly rather than going through
    improvement.validate_improvement, so without its own gate
    forbidden_import_modules would not apply to it at all (#52)."""

    def setup_method(self):
        self.repo_root = Path("/tmp/test_repo")

    @patch("ouroboros.git_ops.create_branch")
    @patch("pathlib.Path.write_text")
    @patch("ouroboros.llm.chat_completion")
    def test_a_blocked_import_is_refused_before_anything_is_written(
        self, mock_llm, mock_write, mock_branch
    ):
        mock_llm.return_value = (json.dumps({
            "explanation": "sneaky",
            "changes": [
                {"file_path": "src/a.py", "new_content": "import pickle\n"}
            ],
        }), None)

        issue = GitHubIssue(123, "title", "body", "author", "url")
        result = apply_github_fix(MagicMock(), issue, {}, self.repo_root)

        assert result.status == "failed"
        assert "pickle" in result.error
        mock_write.assert_not_called()
        # Refused before the branch exists, so there is nothing to clean up.
        mock_branch.assert_not_called()

    @patch("ouroboros.git_ops.create_branch")
    @patch("pathlib.Path.write_text")
    @patch("ouroboros.llm.chat_completion")
    def test_a_blocked_import_in_a_generated_test_is_also_refused(
        self, mock_llm, mock_write, mock_branch
    ):
        """new_tests is written by the same loop and was equally ungated."""
        mock_llm.return_value = (json.dumps({
            "explanation": "fix",
            "changes": [{"file_path": "src/a.py", "new_content": "VALUE = 1\n"}],
            "new_tests": [
                {"file_path": "tests/test_a.py", "content": "import ctypes\n"}
            ],
        }), None)

        issue = GitHubIssue(123, "title", "body", "author", "url")
        result = apply_github_fix(MagicMock(), issue, {}, self.repo_root)

        assert result.status == "failed"
        assert "ctypes" in result.error
        mock_write.assert_not_called()

    @patch("ouroboros.git_ops.create_branch")
    @patch("pathlib.Path.write_text")
    @patch("ouroboros.llm.chat_completion")
    def test_a_non_python_change_is_not_parsed(self, mock_llm, mock_write, mock_branch):
        mock_llm.return_value = (json.dumps({
            "explanation": "docs",
            "changes": [
                {"file_path": "docs/x.md", "new_content": "not python: import pickle"}
            ],
        }), None)

        issue = GitHubIssue(123, "title", "body", "author", "url")
        with patch("ouroboros.test_runner.run_tests") as mock_test, \
             patch("ouroboros.git_ops.current_branch", return_value="main"), \
             patch("ouroboros.git_ops.checkout_branch"), \
             patch("ouroboros.git_ops.delete_branch"):
            mock_test.return_value = MagicMock(failed=0, errors=0)
            result = apply_github_fix(MagicMock(), issue, {}, self.repo_root)

        assert "Import policy" not in (result.error or "")
        mock_write.assert_called()
