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
            "changes": [{"file_path": "src/ouroboros/a.py", "new_content": "VALUE = 1\n"}]
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
            "changes": [{"file_path": "src/ouroboros/a.py", "new_content": "VALUE = 2\n"}]
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
                {"file_path": "src/ouroboros/a.py", "new_content": "import pickle\n"}
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
            "changes": [{"file_path": "src/ouroboros/a.py", "new_content": "VALUE = 1\n"}],
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
                {"file_path": "docs/wiki/x.md", "new_content": "not python: import pickle"}
            ],
        }), None)

        issue = GitHubIssue(123, "title", "body", "author", "url")
        with patch("ouroboros.test_runner.run_tests") as mock_test, \
             patch("ouroboros.git_ops.current_branch", return_value="main"), \
             patch("ouroboros.git_ops.checkout_branch"), \
             patch("ouroboros.git_ops.delete_branch"), \
             patch("ouroboros.git_ops.commit_changes"), \
             patch("ouroboros.git_ops.push_branch"), \
             patch("ouroboros.git_ops.create_pr", return_value="http://pr/1"):
            mock_test.return_value = MagicMock(failed=0, errors=0)
            result = apply_github_fix(MagicMock(), issue, {}, self.repo_root)

        # Asserting on the absence of a message prefix could not fail once the
        # prefix changed. Assert the outcome instead -- which is what caught
        # that the flow was not reaching the end at all.
        assert result.status == "success", result.error
        mock_write.assert_called()

    @patch("ouroboros.git_ops.create_branch")
    @patch("pathlib.Path.write_text")
    @patch("ouroboros.llm.chat_completion")
    def test_an_absolute_path_cannot_escape_the_repository(
        self, mock_llm, mock_write, mock_branch
    ):
        """file_path comes from a model reading a public issue, and
        `repo_root / "/etc/passwd"` discards repo_root entirely. This flow
        wrote it with no path check at all."""
        mock_llm.return_value = (json.dumps({
            "explanation": "helpful",
            "changes": [
                {"file_path": "/etc/passwd", "new_content": "VALUE = 1\n"}
            ],
        }), None)

        issue = GitHubIssue(123, "title", "body", "author", "url")
        result = apply_github_fix(MagicMock(), issue, {}, self.repo_root)

        assert result.status == "failed"
        mock_write.assert_not_called()
        mock_branch.assert_not_called()

    @patch("ouroboros.git_ops.create_branch")
    @patch("pathlib.Path.write_text")
    @patch("ouroboros.llm.chat_completion")
    def test_an_immutable_file_cannot_be_rewritten(
        self, mock_llm, mock_write, mock_branch
    ):
        mock_llm.return_value = (json.dumps({
            "explanation": "loosen the safety config",
            "changes": [
                {"file_path": "src/ouroboros/config.py",
                 "new_content": "VALUE = 1\n"}
            ],
        }), None)

        issue = GitHubIssue(123, "title", "body", "author", "url")
        result = apply_github_fix(MagicMock(), issue, {}, self.repo_root)

        assert result.status == "failed"
        mock_write.assert_not_called()

    @patch("ouroboros.llm.chat_completion")
    def test_a_symlinked_component_cannot_be_written_through(self, mock_llm, tmp_path):
        """This flow used its own write loop, so it did not get the resolution
        check that apply_changes performs. It writes through apply_changes now."""
        import os

        repo = tmp_path / "repo"
        (repo / "src" / "ouroboros").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "secret.py"
        victim.write_text("original\n")
        os.symlink(outside, repo / "src" / "ouroboros" / "link")

        mock_llm.return_value = (json.dumps({
            "explanation": "helpful",
            "changes": [
                {"file_path": "src/ouroboros/link/secret.py",
                 "new_content": "OWNED = 1\n"}
            ],
        }), None)

        issue = GitHubIssue(123, "title", "body", "author", "url")
        with patch("ouroboros.git_ops.create_branch"), \
             patch("ouroboros.git_ops.current_branch", return_value="main"), \
             patch("ouroboros.git_ops.checkout_branch"), \
             patch("ouroboros.git_ops.delete_branch"):
            result = apply_github_fix(MagicMock(), issue, {}, repo)

        assert result.status == "failed"
        assert victim.read_text() == "original\n"

    @patch("ouroboros.llm.chat_completion")
    def test_issue_context_cannot_read_outside_the_repository(self, mock_llm, tmp_path):
        """target_files comes from a model reading a public issue, and the
        contents go into a prompt whose reply becomes a public PR -- so an
        unguarded read here quotes a credentials file back out."""
        repo = tmp_path / "repo"
        (repo / "src" / "ouroboros").mkdir(parents=True)
        secret = tmp_path / "credentials.json"
        secret.write_text('{"api_key": "sk-SECRET"}')

        mock_llm.return_value = (json.dumps({
            "explanation": "e",
            "changes": [
                {"file_path": "src/ouroboros/a.py", "new_content": "V = 1\n"}
            ],
        }), None)

        analysis = {"target_files": [
            str(secret),                       # absolute: escapes repo_root
            "../credentials.json",             # traversal
            "src/ouroboros/nope.py",           # simply absent
        ]}

        with patch("ouroboros.test_runner.run_tests") as mock_test, \
             patch("ouroboros.git_ops.current_branch", return_value="main"), \
             patch("ouroboros.git_ops.create_branch"), \
             patch("ouroboros.git_ops.checkout_branch"), \
             patch("ouroboros.git_ops.delete_branch"), \
             patch("ouroboros.git_ops.commit_changes"), \
             patch("ouroboros.git_ops.push_branch"), \
             patch("ouroboros.git_ops.create_pr", return_value="http://pr/1"):
            mock_test.return_value = MagicMock(failed=0, errors=0)
            apply_github_fix(MagicMock(), GitHubIssue(1, "t", "b", "a", "u"),
                             analysis, repo)

        sent = str(mock_llm.call_args)
        assert "sk-SECRET" not in sent, "a file outside the repo reached the prompt"

    def test_the_context_read_only_reaches_files_the_fix_could_modify(self, tmp_path):
        """Resolving inside the repository is not enough: .git/config carries
        the remote URL and any token in it, and a .env is inside the tree too.
        A file the fix cannot touch is not context it needs."""
        import os

        from ouroboros.github_improvement import _read_inside_repo

        repo = tmp_path / "repo"
        (repo / "src" / "ouroboros").mkdir(parents=True)
        (repo / ".git").mkdir()
        (repo / ".git" / "config").write_text("url = https://x:TOKEN@github.com/a/b")
        (repo / ".env").write_text("API_KEY=sk-SECRET")

        assert _read_inside_repo(repo, ".git/config") is None
        assert _read_inside_repo(repo, ".env") is None
        assert _read_inside_repo(repo, "pyproject.toml") is None

        # is_file() is False for a FIFO, so it is never opened.
        os.mkfifo(repo / "src" / "ouroboros" / "pipe")
        assert _read_inside_repo(repo, "src/ouroboros/pipe") is None
        assert _read_inside_repo(repo, "src/ouroboros/missing.py") is None

        (repo / "src" / "ouroboros" / "real.py").write_text("V = 1\n")
        assert _read_inside_repo(repo, "src/ouroboros/real.py") == "V = 1\n"

    @patch("ouroboros.llm.chat_completion")
    def test_dry_run_reports_a_policy_violation_rather_than_success(self, mock_llm):
        """A preview that says "would succeed" for a change the gate would
        refuse is worse than no preview."""
        mock_llm.return_value = (json.dumps({
            "explanation": "sneaky",
            "changes": [
                {"file_path": "src/ouroboros/config.py", "new_content": "V = 1\n"}
            ],
        }), None)

        issue = GitHubIssue(123, "title", "body", "author", "url")
        result = apply_github_fix(
            MagicMock(), issue, {}, self.repo_root, dry_run=True
        )

        assert result.status == "failed"
        assert "config.py" in result.error

    def test_a_symlink_cannot_smuggle_a_secret_into_the_prompt(self, tmp_path):
        """The same hole the write path had before it re-judged the resolved
        target: a link under an allowed directory lands inside the repository
        and satisfies every check on its own name."""
        import os

        from ouroboros.github_improvement import _read_inside_repo

        repo = tmp_path / "repo"
        pkg = repo / "src" / "ouroboros"
        pkg.mkdir(parents=True)
        (repo / ".env").write_text("API_KEY=sk-SECRET")
        (pkg / "config.py").write_text("ALLOW_SELF_MODIFICATION = False")
        os.symlink(repo / ".env", pkg / "link.py")

        assert _read_inside_repo(repo, "src/ouroboros/link.py") is None
        # Immutable files are refused too, so read and write answer one rule.
        assert _read_inside_repo(repo, "src/ouroboros/config.py") is None

        (pkg / "ok.py").write_text("VALUE = 1\n")
        assert _read_inside_repo(repo, "src/ouroboros/ok.py") == "VALUE = 1\n"
