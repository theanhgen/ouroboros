"""Autonomous issue scouting for code improvements."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import git_ops
from .codebase import get_codebase_summary
from .evaluation import load_history
from .improvement import ImprovementTask, identify_improvements
from .model_defaults import DEFAULT_OPENAI_MODEL
from .test_runner import run_tests

log = logging.getLogger(__name__)


@dataclass
class IssueScoutResult:
    status: str
    message: str
    issue_url: Optional[str] = None
    task: Optional[ImprovementTask] = None


def _task_issue_marker(task: ImprovementTask) -> str:
    return git_ops.make_auto_issue_marker(
        task.task_type,
        task.description,
        task.target_files,
    )


def _build_issue_title(task: ImprovementTask) -> str:
    description = task.description.strip() or "unspecified improvement"
    return f"[ouroboros] scout {task.task_type}: {description[:72]}"


def _build_issue_body(task: ImprovementTask, test_summary: str, marker: str) -> str:
    lines = [
        "## Autonomous Improvement Opportunity",
        "",
        f"**Task type**: {task.task_type}",
        f"**Description**: {task.description}",
        "",
        "### Why this looks worth doing",
        task.evidence or "No extra evidence provided.",
        "",
        "### Candidate files",
    ]

    if task.target_files:
        for file_path in task.target_files:
            lines.append(f"- `{file_path}`")
    else:
        lines.append("- No specific files suggested")

    lines.extend(
        [
            "",
            "### Current test state",
            f"- {test_summary}",
            "",
            "---",
            "Opened automatically by the issue-scouting loop so the opportunity is tracked before any code is changed.",
            marker,
        ]
    )
    return "\n".join(lines)


def run_issue_scouting_cycle(
    client: Any,
    repo_root: Path,
    model: str = DEFAULT_OPENAI_MODEL,
    dry_run: bool = False,
) -> IssueScoutResult:
    """Identify one improvement opportunity and track it as a GitHub issue."""
    history = load_history(repo_root)
    codebase_summary = get_codebase_summary(repo_root)
    test_results = run_tests(repo_root)

    task = identify_improvements(
        client,
        codebase_summary,
        test_results,
        history,
        model=model,
    )
    if task is None:
        return IssueScoutResult("idle", "No improvements identified.")

    marker = _task_issue_marker(task)
    existing_url = git_ops.find_open_issue_by_marker(repo_root, marker)
    if existing_url:
        return IssueScoutResult(
            "duplicate",
            "Matching scout issue already exists.",
            issue_url=existing_url,
            task=task,
        )

    if dry_run:
        return IssueScoutResult(
            "dry_run",
            f"Would open issue for [{task.task_type}] {task.description}",
            task=task,
        )

    title = _build_issue_title(task)
    body = _build_issue_body(task, test_results.summary(), marker)
    try:
        issue_url = git_ops.create_issue(repo_root, title, body)
    except Exception as exc:
        log.exception("Failed to create scout issue")
        return IssueScoutResult(
            "failed",
            f"Failed to create scout issue: {exc}",
            task=task,
        )

    return IssueScoutResult(
        "created",
        f"Opened issue for [{task.task_type}] {task.description}",
        issue_url=issue_url,
        task=task,
    )
