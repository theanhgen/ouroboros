"""GitHub issue resolution engine -- autonomously fix issues from the repo."""

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import git_ops, llm, prompts, test_runner
from .codebase import get_repo_root, read_file_raw, get_function_signatures, list_source_files
from .model_defaults import DEFAULT_OPENAI_MODEL

log = logging.getLogger(__name__)


@dataclass
class GitHubIssue:
    id: int
    title: str
    body: str
    author: str
    url: str


@dataclass
class IssueResolutionResult:
    issue_id: int
    status: str  # "success", "failed", "skipped", "not_found"
    pr_url: Optional[str] = None
    error: Optional[str] = None
    description: Optional[str] = None


def get_open_issues(repo_root: Path) -> List[GitHubIssue]:
    """List open GitHub issues using the gh CLI."""
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--state", "open", "--json", "number,title,body,author,url"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        data = json.loads(result.stdout)
        issues = []
        for item in data:
            issues.append(GitHubIssue(
                id=item["number"],
                title=item["title"],
                body=item["body"],
                author=item["author"]["login"] if isinstance(item["author"], dict) else item["author"],
                url=item["url"]
            ))
        return issues
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as e:
        log.warning("Could not fetch GitHub issues: %s", e)
        return []


def analyze_issue(client: Any, issue: GitHubIssue, repo_root: Path, model: str = DEFAULT_OPENAI_MODEL) -> Dict[str, Any]:
    """Analyze a GitHub issue and formulate a fix plan."""
    # Build context: issue info + codebase signatures
    all_files = list_source_files(repo_root)
    file_info = []
    for f in all_files[:10]:  # Limit context
        sigs = get_function_signatures(f)
        file_info.append(f"File: {f.relative_to(repo_root)}\nSignatures: {sigs}")

    codebase_context = "\n".join(file_info)

    user_prompt = f"""
Issue #{issue.id}: {issue.title}
Author: {issue.author}
URL: {issue.url}

Description:
{issue.body}

Codebase context (relevant files):
{codebase_context}
"""

    content, _ = llm.chat_completion(
        client,
        system_prompt=prompts.load_github_issue_analysis_prompt(),
        user_prompt=user_prompt,
        model=model,
        response_format={"type": "json_object"}
    )

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        log.error("Failed to parse issue analysis JSON")
        return {}


def _read_inside_repo(repo_root: Path, file_path: str) -> Optional[str]:
    """Read a file only if it really is one, inside the repository.

    Both paths this function guards come from a model reading a public issue.
    `repo_root / "/etc/passwd"` is "/etc/passwd", and the contents go into a
    prompt whose reply becomes a public PR -- so an unguarded read here is a
    way to get a credentials file quoted back out. A lexical check is not
    enough: a symlink component resolves elsewhere while looking local.

    Restricted to the paths the agent is allowed to modify. Resolving inside
    the repository is not enough on its own: .git/config carries the remote
    URL and any token embedded in it, and a .env is inside the tree too. "Read
    only what you may write" is the bound that makes sense here -- a file the
    fix cannot touch is not context it needs.

    is_file() is what keeps a FIFO or a character device from hanging the
    process or reading forever.
    """
    from .config import SafetyConfig
    from .policies import is_within_allowed_paths

    if not is_within_allowed_paths(
        file_path, SafetyConfig().allowed_modification_paths
    ):
        return None
    try:
        root = repo_root.resolve()
        full = (repo_root / file_path).resolve()
        if full != root and root not in full.parents:
            return None
        if not full.is_file():
            return None
        return full.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def apply_github_fix(
    client: Any,
    issue: GitHubIssue,
    analysis: Dict[str, Any],
    repo_root: Path,
    model: str = DEFAULT_OPENAI_MODEL,
    dry_run: bool = False
) -> Optional[IssueResolutionResult]:
    """Generate and apply a fix for a GitHub issue."""
    # Read the target files for context
    target_files = analysis.get("target_files", [])
    file_contents = {}
    for f_rel in target_files:
        contents = _read_inside_repo(repo_root, f_rel)
        if contents is not None:
            file_contents[f_rel] = contents
        else:
            log.warning(
                "[github] Refusing to read %r for issue #%d context",
                f_rel, issue.id,
            )

    user_prompt = f"""
Issue #{issue.id}: {issue.title}
Analysis: {json.dumps(analysis, indent=2)}

Current file contents:
{json.dumps(file_contents, indent=2)}

Please provide the fix as a JSON object with 'explanation', 'changes' (list of {{file_path, new_content}}), and optionally 'new_tests'.
"""

    content, _ = llm.chat_completion(
        client,
        system_prompt=prompts.load_github_issue_fix_prompt(),
        user_prompt=user_prompt,
        model=model,
        response_format={"type": "json_object"}
    )

    try:
        fix_data = json.loads(content)
    except json.JSONDecodeError:
        return IssueResolutionResult(issue.id, "failed", error="Failed to parse fix JSON")

    # This flow writes files directly instead of going through
    # validate_improvement, so it had no policy gate at all: file_path comes
    # from a model reading a public issue, and `repo_root / "/etc/passwd"`
    # discards repo_root entirely. Run the same checks the other flows run --
    # scope, immutable files, change size and imports -- before anything is
    # written.
    from .config import SafetyConfig
    from .improvement import CodeChange, _validate_changes, apply_changes

    safety = SafetyConfig()
    proposed = []
    for change in fix_data.get("changes", []):
        proposed.append((change.get("file_path", ""), change.get("new_content", "")))
    for test in fix_data.get("new_tests", []):
        proposed.append((test.get("file_path", ""), test.get("content", "")))

    gated = []
    for path, content in proposed:
        # Gathering size accounting is not a reason to open a file the change
        # is about to be refused for naming.
        original = _read_inside_repo(repo_root, path) or ""
        gated.append(
            CodeChange(
                file_path=path,
                original_content=original,
                new_content=content,
                description=f"issue #{issue.id}",
            )
        )

    violations = _validate_changes(gated, safety) if gated else []
    if violations:
        # Refused before the branch exists, so nothing needs cleaning up.
        log.warning(
            "Fix for issue #%d rejected by policy: %s",
            issue.id, "; ".join(violations),
        )
        return IssueResolutionResult(
            issue.id, "failed", error="Policy: " + "; ".join(violations),
        )

    if dry_run:
        log.info("[dry-run] Would apply fix for issue #%d: %s", issue.id, fix_data.get("explanation"))
        return IssueResolutionResult(issue.id, "success", description=fix_data.get("explanation"))

    # Apply changes on a new branch
    branch_name = git_ops.make_branch_name(f"fix_issue_{issue.id}")
    try:
        original_branch = git_ops.current_branch(repo_root)
        git_ops.create_branch(repo_root, branch_name)

        # Through apply_changes, not a local write loop. The policy checks
        # above are lexical and cannot see a symlink; apply_changes resolves
        # each path and refuses one that lands outside the repository.
        apply_changes(gated, repo_root, safety)
        affected_files = [c.file_path for c in gated]

        # Run tests
        test_res = test_runner.run_tests(repo_root)
        if test_res.failed > 0 or test_res.errors > 0:
            log.warning("Fix for issue #%d failed tests, aborting", issue.id)
            git_ops.checkout_branch(repo_root, original_branch)
            git_ops.delete_branch(repo_root, branch_name)
            return IssueResolutionResult(issue.id, "failed", error=f"Tests failed: {test_res.failed}f, {test_res.errors}e")

        # Commit and PR
        commit_msg = (
            f"ouroboros: fix issue #{issue.id} - {issue.title}\n\n"
            f"{fix_data.get('explanation')}"
        )
        git_ops.commit_changes(repo_root, commit_msg, affected_files)
        git_ops.push_branch(repo_root, branch_name)

        pr_body = f"""## Fixed Issue #{issue.id}

**Description:** {fix_data.get('explanation')}

**Plan:**
{chr(10).join('- ' + s for s in analysis.get('plan', []))}

**Reproduction:**
{analysis.get('reproduction', 'N/A')}

🤖 Generated autonomously by Ouroboros"""

        pr_url = git_ops.create_pr(
            repo_root,
            title=f"[ouroboros] fix #{issue.id}: {issue.title}",
            body=pr_body,
            head=branch_name
        )

        git_ops.checkout_branch(repo_root, original_branch)
        return IssueResolutionResult(issue.id, "success", pr_url=pr_url, description=fix_data.get("explanation"))

    except Exception as e:
        log.exception("Error applying fix for issue #%d", issue.id)
        # Attempt recovery
        try:
            git_ops.checkout_main(repo_root)
        except: pass
        return IssueResolutionResult(issue.id, "failed", error=str(e))


def run_github_improvement_cycle(
    client: Any,
    repo_root: Path,
    model: str = DEFAULT_OPENAI_MODEL,
    dry_run: bool = False,
    enable_auto_merge: bool = False,
) -> List[IssueResolutionResult]:
    """One full cycle: find issues -> fix them -> PR."""
    issues = get_open_issues(repo_root)
    if not issues:
        log.info("No open GitHub issues found.")
        return []

    results = []
    for issue in issues:
        log.info("Processing issue #%d: %s", issue.id, issue.title)
        
        # Skip if already has an open improvement PR
        # `is not False` covers None: an indeterminate lookup must not be read
        # as permission to open another PR.
        if git_ops.has_open_improvement_prs(repo_root) is not False:
            log.info("Skipping issue #%d: another improvement PR is open", issue.id)
            results.append(IssueResolutionResult(issue.id, "skipped", description="Other PR open"))
            continue

        analysis = analyze_issue(client, issue, repo_root, model)
        if not analysis or analysis.get("confidence", 0) < 0.6:
            log.info("Skipping issue #%d: low confidence or failed analysis", issue.id)
            results.append(IssueResolutionResult(issue.id, "skipped", description="Low confidence"))
            continue

        result = apply_github_fix(client, issue, analysis, repo_root, model, dry_run)
        if result:
            if result.status == "success" and result.pr_url and enable_auto_merge:
                git_ops.auto_merge_pr(repo_root, result.pr_url)
            results.append(result)

    return results
