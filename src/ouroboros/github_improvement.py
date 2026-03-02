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


def analyze_issue(client: Any, issue: GitHubIssue, repo_root: Path, model: str = "gpt-4o") -> Dict[str, Any]:
    """Analyze a GitHub issue and formulate a fix plan."""
    # Build context: issue info + codebase signatures
    all_files = list_source_files(repo_root)
    file_info = []
    for f in all_files[:10]: # Limit context
        sigs = get_function_signatures(f)
        file_info.append(f"File: {f.relative_to(repo_root)}
Signatures: {sigs}")

    user_prompt = f"""
Issue #{issue.id}: {issue.title}
Author: {issue.author}
URL: {issue.url}

Description:
{issue.body}

Codebase context (relevant files):
{"
".join(file_info)}
"""

    response = llm.chat_completion(
        client,
        system_prompt=prompts.load_github_issue_analysis_prompt(),
        user_prompt=user_prompt,
        model=model,
        response_format={"type": "json_object"}
    )

    try:
        return json.loads(response)
    except json.JSONDecodeError:
        log.error("Failed to parse issue analysis JSON")
        return {}


def apply_github_fix(
    client: Any,
    issue: GitHubIssue,
    analysis: Dict[str, Any],
    repo_root: Path,
    model: str = "gpt-4o",
    dry_run: bool = False
) -> Optional[IssueResolutionResult]:
    """Generate and apply a fix for a GitHub issue."""
    # Read the target files for context
    target_files = analysis.get("target_files", [])
    file_contents = {}
    for f_rel in target_files:
        f_path = repo_root / f_rel
        if f_path.exists():
            file_contents[f_rel] = read_file_raw(f_path)

    user_prompt = f"""
Issue #{issue.id}: {issue.title}
Analysis: {json.dumps(analysis, indent=2)}

Current file contents:
{json.dumps(file_contents, indent=2)}

Please provide the fix as a JSON object with 'explanation', 'changes' (list of {{file_path, new_content}}), and optionally 'new_tests'.
"""

    response = llm.chat_completion(
        client,
        system_prompt=prompts.load_github_issue_fix_prompt(),
        user_prompt=user_prompt,
        model=model,
        response_format={"type": "json_object"}
    )

    try:
        fix_data = json.loads(response)
    except json.JSONDecodeError:
        return IssueResolutionResult(issue.id, "failed", error="Failed to parse fix JSON")

    if dry_run:
        log.info("[dry-run] Would apply fix for issue #%d: %s", issue.id, fix_data.get("explanation"))
        return IssueResolutionResult(issue.id, "success", description=fix_data.get("explanation"))

    # Apply changes on a new branch
    branch_name = git_ops.make_branch_name(f"fix_issue_{issue.id}")
    try:
        original_branch = git_ops.current_branch(repo_root)
        git_ops.create_branch(repo_root, branch_name)

        affected_files = []
        for change in fix_data.get("changes", []):
            f_path = repo_root / change["file_path"]
            f_path.parent.mkdir(parents=True, exist_ok=True)
            f_path.write_text(change["new_content"], encoding="utf-8")
            affected_files.append(change["file_path"])

        for test in fix_data.get("new_tests", []):
            f_path = repo_root / test["file_path"]
            f_path.parent.mkdir(parents=True, exist_ok=True)
            f_path.write_text(test["content"], encoding="utf-8")
            affected_files.append(test["file_path"])

        # Run tests
        test_res = test_runner.run_tests(repo_root)
        if test_res.failed > 0 or test_res.errors > 0:
            log.warning("Fix for issue #%d failed tests, aborting", issue.id)
            git_ops.checkout_branch(repo_root, original_branch)
            git_ops.delete_branch(repo_root, branch_name)
            return IssueResolutionResult(issue.id, "failed", error=f"Tests failed: {test_res.failed}f, {test_res.errors}e")

        # Commit and PR
        commit_msg = f"ouroboros: fix issue #{issue.id} - {issue.title}

{fix_data.get('explanation')}"
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
    model: str = "gpt-4o",
    dry_run: bool = False
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
        if git_ops.has_open_improvement_prs(repo_root):
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
            results.append(result)

    return results
