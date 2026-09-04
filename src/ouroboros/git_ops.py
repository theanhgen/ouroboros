"""Git and PR operations for the self-improvement workflow."""

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

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


_GIT_C_ESCAPES = {
    "a": b"\a",
    "b": b"\b",
    "f": b"\f",
    "n": b"\n",
    "r": b"\r",
    "t": b"\t",
    "v": b"\v",
    "\\": b"\\",
    '"': b'"',
}


def _decode_git_path(path: str) -> str:
    """Decode a C-quoted path from git output.

    git quotes any path containing spaces, quotes, backslashes or non-ASCII
    bytes, escaping the contents C-style (``\\303\\251`` for a UTF-8 e-acute).
    Unquoted paths are returned unchanged.
    """
    path = path.strip()
    if len(path) < 2 or path[0] != '"' or path[-1] != '"':
        return path

    raw = path[1:-1]
    decoded = bytearray()
    i = 0
    while i < len(raw):
        char = raw[i]
        if char != "\\":
            decoded.extend(char.encode("utf-8"))
            i += 1
            continue

        i += 1
        if i >= len(raw):
            decoded.append(ord("\\"))
            break

        char = raw[i]
        if char in "01234567":
            octal = char
            i += 1
            while i < len(raw) and len(octal) < 3 and raw[i] in "01234567":
                octal += raw[i]
                i += 1
            decoded.append(int(octal, 8))
            continue

        decoded.extend(_GIT_C_ESCAPES.get(char, char.encode("utf-8")))
        i += 1

    return decoded.decode("utf-8", "surrogateescape")


def _git_porcelain_target_path(line: str) -> str:
    """Return the changed path from a git status --porcelain line.

    For renames and copies the line is ``XY <orig> -> <dest>``; the separator
    is only meaningful outside a quoted path, since a filename may itself
    contain " -> ".
    """
    status = line[:2]
    path = line[3:].strip()
    if "R" in status or "C" in status:
        in_quote = False
        escaped = False
        for i, char in enumerate(path):
            if escaped:
                escaped = False
                continue
            if in_quote and char == "\\":
                escaped = True
                continue
            if char == '"':
                in_quote = not in_quote
                continue
            if not in_quote and path.startswith(" -> ", i):
                path = path[i + 4:].strip()
                break
    return _decode_git_path(path)


def _git_porcelain_changes(porcelain: str) -> Iterator[Tuple[str, str]]:
    """Yield ``(status, decoded_path)`` entries from git status porcelain output."""
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        yield line[:2], _git_porcelain_target_path(line)


# Files that the main loop modifies during normal operation.
# A trailing slash means "this directory and everything under it"; every other
# entry names one exact file.
_AUTO_STATE_FILES = [
    "config/state.json",
    "config/improvement_history.json",
    "config/self_improvement_state.json",
    "config/learnings.md",
    "config/metrics.json",
    "docs/wiki/",
]


def _is_auto_state_path(path: str) -> bool:
    """Return True if path is one of the files the loop may auto-commit.

    Exact entries are compared for equality rather than by prefix: matching
    "config/state.json" with startswith would also pull in a sibling like
    config/state.json.backup, and commit_auto_state pushes what it stages.
    """
    for entry in _AUTO_STATE_FILES:
        if entry.endswith("/"):
            if path == entry.rstrip("/") or path.startswith(entry):
                return True
        elif path == entry:
            return True
    return False


def commit_auto_state(repo: Path) -> bool:
    """Commit auto-generated state files so the worktree stays clean.

    Returns True if a commit was created, False if there was nothing to commit.
    """
    porcelain = _git(repo, "status", "--porcelain", check=False).stdout
    if not porcelain.strip():
        return False

    to_add: list[str] = [
        path for _status, path in _git_porcelain_changes(porcelain)
        if _is_auto_state_path(path)
    ]

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


def make_auto_issue_marker(task_type: str, description: str, target_files: List[str]) -> str:
    """Build a stable hidden marker for a generated issue."""
    payload = json.dumps(
        {
            "description": description,
            "target_files": target_files,
            "task_type": task_type,
        },
        sort_keys=True,
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"<!-- ouroboros:auto-issue:{digest} -->"


# gh pr list defaults to 30 results. This guard has to see every open
# improvement PR, not the first page of them.
_PR_LIST_LIMIT = 200


def has_open_improvement_prs(repo: Path) -> Optional[bool]:
    """Whether an open ouroboros/improve- PR exists.

    Returns True if one exists, False if none does, and None if that could not
    be determined -- gh missing, erroring, or hanging.

    None rather than False for the failure case: this gates whether another
    autonomous cycle may start, and callers read a plain False as permission.
    A GitHub outage would then let the agent open a second PR for work already
    in flight, which is the exact situation the guard exists to prevent.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--limit", str(_PR_LIST_LIMIT),
             "--json", "headRefName", "-q", '.[].headRefName'],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        branches = result.stdout.strip().splitlines()
        if any(b.startswith("ouroboros/improve-") for b in branches):
            return True
        if len(branches) >= _PR_LIST_LIMIT:
            # The page was full, so there may be more we did not see. "None of
            # the first 200" is not "none".
            log.warning(
                "Open PR list hit the %d result limit; cannot rule out an "
                "open improvement PR", _PR_LIST_LIMIT,
            )
            return None
        return False
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        log.warning("Could not check open PRs (gh CLI unavailable or timed out)")
        return None


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
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def auto_merge_pr(repo: Path, pr_url: str, strategy: str = "squash") -> bool:
    """Enable auto-merge on a PR. Returns True on success.

    Uses --auto so the PR merges once all checks pass. When the repository has
    auto-merge disabled there is no safe fallback: merging without --auto lands
    the PR immediately, before CI has had a chance to run, which is the opposite
    of what this function promises. In that case it refuses and returns False,
    leaving the PR open for a human to merge.
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
        stderr = e.stderr or ""
        # --auto requires allow_auto_merge on the repository. Without it the
        # only remaining option is an unguarded `gh pr merge`, which merges now
        # and waits for nothing -- so refuse rather than skip the checks.
        if "auto-merge" in stderr.lower() or "not allowed" in stderr.lower():
            log.error(
                "Auto-merge is disabled on this repository; refusing to merge %s "
                "without waiting for checks. Leaving the PR open. Enable it with: "
                "gh api -X PATCH repos/OWNER/REPO -f allow_auto_merge=true",
                pr_url,
            )
            return False
        log.warning("Failed to enable auto-merge for %s: %s", pr_url, stderr.strip())
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.warning("Could not auto-merge PR %s (gh CLI unavailable or timeout)", pr_url)
        return False


def get_pr_checks_status(repo: Path, pr_url: str) -> Optional[str]:
    """Get the combined CI checks status for a PR.

    Returns 'pass', 'fail', 'pending', or None when the status is unknown.

    An empty check list means the checks have not started, not that they
    passed: jq's all() is true on an empty array, so the query tests length
    first. gh signals the same thing out of band -- it exits 8 while checks
    are pending and 1 with "no checks reported" when a PR has none at all,
    printing nothing on stdout in either case. Neither is a green PR, so both
    read 'pending'.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "checks", pr_url, "--json", "state", "-q",
             '[.[] | .state] | if length == 0 then "pending"'
             ' elif any(. == "FAILURE") then "fail"'
             ' elif all(. == "SUCCESS") then "pass" else "pending" end'],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    status = (result.stdout or "").strip()
    if status in ("pass", "fail", "pending"):
        return status
    if result.returncode == 8 or "no checks reported" in (result.stderr or "").lower():
        return "pending"
    return None


def get_pr_feedback(repo: Path, pr_url: str, max_chars: int = 2000) -> Optional[str]:
    """Extract review and comment bodies from a PR, truncated.

    Returns "" when the PR genuinely has no feedback, and None when it could
    not be fetched. The caller records this against a terminal outcome and
    then stops polling that record, so collapsing a timeout into "" would
    discard the review permanently.
    """
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
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        log.debug("Could not fetch PR feedback for %s", pr_url)
        return None
