"""Git and PR operations for the self-improvement workflow."""

import json
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


# Files that the main loop modifies during normal operation.
_AUTO_STATE_FILES = [
    "config/state.json",
    "config/improvement_history.json",
    "config/self_improvement_state.json",
    "config/learnings.md",
    "config/metrics.json",
    "docs/wiki/",
]


def commit_auto_state(repo: Path) -> bool:
    """Commit auto-generated state files so the worktree stays clean.

    Returns True if a commit was created, False if there was nothing to commit.
    """
    porcelain = _git(repo, "status", "--porcelain", check=False).stdout
    if not porcelain.strip():
        return False

    to_add: list[str] = []
    for line in porcelain.splitlines():
        # porcelain format: XY <path>  or  XY <orig> -> <path>
        path = line[3:].split(" -> ")[-1]
        if any(path.startswith(prefix) or path == prefix.rstrip("/") for prefix in _AUTO_STATE_FILES):
            to_add.append(path)

    if not to_add:
        return False

    _git(repo, "add", *to_add)
    # Only commit if staging actually produced changes
    staged = _git(repo, "diff", "--cached", "--name-only", check=False).stdout.strip()
    if not staged:
        return False

    _git(repo, "commit", "-m", "chore: auto-commit state files before improvement cycle")
    _git(repo, "push", "origin", current_branch(repo), timeout=60)
    log.info("Auto-committed state files: %s", ", ".join(to_add))
    return True


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


def pull_latest(repo: Path) -> bool:
    """Pull latest changes from origin for the current branch.

    Returns True if source files (src/) were updated.
    """
    try:
        head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "pull", "--ff-only", "origin", current_branch(repo), timeout=60)
        head_after = _git(repo, "rev-parse", "HEAD").stdout.strip()

        if head_before == head_after:
            return False

        log.info("Pulled latest changes from origin (%s -> %s)", head_before[:8], head_after[:8])
        diff = _git(repo, "diff", "--name-only", head_before, head_after).stdout
        return any(line.startswith("src/") for line in diff.splitlines())
    except subprocess.CalledProcessError:
        log.warning("git pull --ff-only failed (local diverged?), skipping")
        return False


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


def create_issue(repo: Path, title: str, body: str) -> str:
    """Create a GitHub issue using the gh CLI. Returns the issue URL."""
    result = subprocess.run(
        ["gh", "issue", "create", "--title", title, "--body", body],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout.strip()


def find_open_issue_by_marker(repo: Path, marker: str) -> Optional[str]:
    """Return the URL of an open issue containing a hidden marker, if any."""
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--state", "open", "--limit", "100", "--json", "body,url"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        issues = json.loads(result.stdout)
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
        log.warning("Could not list open issues (gh CLI unavailable?)")
        return None

    for issue in issues:
        if marker in issue.get("body", ""):
            return issue.get("url")
    return None


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


def auto_merge_pr(repo: Path, pr_url: str, strategy: str = "squash") -> bool:
    """Enable auto-merge on a PR. Returns True on success.

    Uses --auto so the PR merges once all checks pass.
    Falls back to immediate merge if auto-merge is not available on the repo.
    """
    try:
        subprocess.run(
            ["gh", "pr", "merge", pr_url, f"--{strategy}", "--auto"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        log.info("Auto-merge enabled for PR: %s", pr_url)
        return True
    except subprocess.CalledProcessError as e:
        # --auto may not be available (requires branch protection), try immediate merge
        if "auto-merge" in e.stderr.lower() or "not allowed" in e.stderr.lower():
            log.info("Auto-merge not available, attempting immediate merge for %s", pr_url)
            try:
                subprocess.run(
                    ["gh", "pr", "merge", pr_url, f"--{strategy}"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=60,
                )
                log.info("Immediately merged PR: %s", pr_url)
                return True
            except subprocess.CalledProcessError:
                log.warning("Immediate merge also failed for %s", pr_url)
                return False
        log.warning("Failed to enable auto-merge for %s: %s", pr_url, e.stderr.strip())
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.warning("Could not auto-merge PR %s (gh CLI unavailable or timeout)", pr_url)
        return False


def get_pr_checks_status(repo: Path, pr_url: str) -> Optional[str]:
    """Get the combined CI checks status for a PR.

    Returns 'pass', 'fail', 'pending', or None.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "checks", pr_url, "--json", "state", "-q",
             '[.[] | .state] | if all(. == "SUCCESS") then "pass" elif any(. == "FAILURE") then "fail" else "pending" end'],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return result.stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def get_pr_feedback(repo: Path, pr_url: str, max_chars: int = 2000) -> str:
    """Extract review and comment bodies from a PR. Returns concatenated text, truncated."""
    try:
        result = subprocess.run(
            [
                "gh", "pr", "view", pr_url,
                "--json", "reviews,comments",
                "-q", '[.reviews[].body, .comments[].body] | map(select(. != null and . != "")) | join("\n---\n")',
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        text = result.stdout.strip()
        if len(text) > max_chars:
            text = text[:max_chars]
        return text
    except (subprocess.CalledProcessError, FileNotFoundError):
        log.debug("Could not fetch PR feedback for %s", pr_url)
        return ""
