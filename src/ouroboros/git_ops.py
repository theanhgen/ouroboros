"""Git and PR operations for the self-improvement workflow."""

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


def _safe_git_env() -> Dict[str, str]:
    """Return env dict with git author/committer set for the bot."""
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "ouroboros-bot")
    env.setdefault("GIT_AUTHOR_EMAIL", "ouroboros-bot@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "ouroboros-bot")
    env.setdefault("GIT_COMMITTER_EMAIL", "ouroboros-bot@localhost")
    return env


def _git(repo: Path, *args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a git command in the given repo."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_safe_git_env(),
        check=check,
        timeout=timeout,
    )


def is_clean(repo: Path) -> bool:
    """Return True if the working tree has no uncommitted changes."""
    result = _git(repo, "status", "--porcelain", check=False)
    return result.stdout.strip() == ""


def current_branch(repo: Path) -> str:
    """Return the name of the current branch."""
    result = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout.strip()


def create_branch(repo: Path, name: str) -> None:
    """Create a new branch from the current HEAD."""
    _git(repo, "checkout", "-b", name)


def checkout_branch(repo: Path, name: str) -> None:
    """Check out an existing branch."""
    _git(repo, "checkout", name)


def checkout_main(repo: Path) -> None:
    """Check out the main branch."""
    # Try 'main' first, fall back to 'master'
    result = _git(repo, "checkout", "main", check=False)
    if result.returncode != 0:
        _git(repo, "checkout", "master")


def delete_branch(repo: Path, name: str) -> None:
    """Delete a local branch."""
    _git(repo, "branch", "-D", name, check=False)


def commit_changes(repo: Path, message: str, files: List[str]) -> str:
    """Stage specified files and commit. Returns the commit hash."""
    if not files:
        raise ValueError("No files to commit")
    _git(repo, "add", *files)
    _git(repo, "commit", "-m", message)
    result = _git(repo, "rev-parse", "HEAD")
    return result.stdout.strip()


def push_branch(repo: Path, branch: str) -> None:
    """Push a branch to origin."""
    _git(repo, "push", "-u", "origin", branch, timeout=60)


def create_pr(
    repo: Path,
    title: str,
    body: str,
    base: str = "main",
    head: Optional[str] = None,
) -> str:
    """Create a GitHub PR using the gh CLI. Returns the PR URL."""
    cmd = [
        "gh", "pr", "create",
        "--title", title,
        "--body", body,
        "--base", base,
    ]
    if head:
        cmd.extend(["--head", head])

    result = subprocess.run(
        cmd,
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    # gh pr create prints the URL on stdout
    return result.stdout.strip()


def has_open_improvement_prs(repo: Path) -> bool:
    """Check if there are any open PRs with the ouroboros/improve- prefix."""
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--json", "headRefName", "-q",
             '.[].headRefName'],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        branches = result.stdout.strip().splitlines()
        return any(b.startswith("ouroboros/improve-") for b in branches)
    except (subprocess.CalledProcessError, FileNotFoundError):
        log.warning("Could not check open PRs (gh CLI unavailable?)")
        return False


def make_branch_name(task_type: str) -> str:
    """Generate a branch name like ouroboros/improve-fix_test-1706000000."""
    ts = int(time.time())
    return f"ouroboros/improve-{task_type}-{ts}"


def get_pr_status(repo: Path, branch: str) -> Optional[str]:
    """Get the status of a PR for a given branch. Returns 'MERGED', 'CLOSED', 'OPEN', or None."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "state", "-q", ".state"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
