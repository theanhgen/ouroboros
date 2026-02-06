"""Outcome tracking for self-improvement attempts."""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from . import git_ops
from .codebase import get_repo_root

log = logging.getLogger(__name__)

HISTORY_FILE = "config/improvement_history.json"


@dataclass
class EvaluationRecord:
    task_id: str
    task_type: str
    description: str
    test_delta: dict = field(default_factory=dict)  # {before: {passed, failed}, after: {passed, failed}}
    pr_url: str = ""
    outcome: str = "pending"  # pending | merged | closed | reverted
    feedback: str = ""
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "EvaluationRecord":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _history_path(repo_root: Optional[Path] = None) -> Path:
    root = repo_root or get_repo_root()
    return root / HISTORY_FILE


def record_improvement(result: "ImprovementResult", repo_root: Optional[Path] = None) -> None:
    """Append an improvement result to the history file."""
    from .improvement import ImprovementResult  # avoid circular import

    path = _history_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    history = load_history(repo_root)
    record = EvaluationRecord(
        task_id=result.task.task_id,
        task_type=result.task.task_type,
        description=result.task.description,
        test_delta={
            "before": {
                "passed": result.test_before.passed if result.test_before else 0,
                "failed": result.test_before.failed if result.test_before else 0,
            },
            "after": {
                "passed": result.test_after.passed if result.test_after else 0,
                "failed": result.test_after.failed if result.test_after else 0,
            },
        },
        pr_url=result.pr_url or "",
        outcome=result.status,
        timestamp=time.time(),
    )
    history.append(record)

    with open(path, "w", encoding="utf-8") as f:
        json.dump([r.to_dict() for r in history], f, indent=2)


def load_history(repo_root: Optional[Path] = None) -> List[EvaluationRecord]:
    """Load improvement history from disk."""
    path = _history_path(repo_root)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [EvaluationRecord.from_dict(d) for d in data]
    except (json.JSONDecodeError, KeyError):
        log.warning("Corrupt improvement history file, returning empty")
        return []


def check_pr_outcomes(repo_root: Optional[Path] = None) -> List[EvaluationRecord]:
    """Poll open PRs and update their outcomes in history."""
    root = repo_root or get_repo_root()
    history = load_history(root)
    updated = False

    for record in history:
        if record.outcome != "pending" or not record.pr_url:
            continue

        # Extract branch name from PR URL or use task info
        # gh pr view can take a URL directly
        try:
            import subprocess
            result = subprocess.run(
                ["gh", "pr", "view", record.pr_url, "--json", "state", "-q", ".state"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            state = result.stdout.strip()
            if state in ("MERGED", "CLOSED"):
                record.outcome = state.lower()
                updated = True
                log.info("PR %s outcome updated: %s", record.pr_url, record.outcome)
        except Exception:
            log.debug("Could not check PR status for %s", record.pr_url)

    if updated:
        path = _history_path(root)
        with open(path, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in history], f, indent=2)

    return history


def improvements_today(repo_root: Optional[Path] = None) -> int:
    """Count improvements attempted in the last 24 hours."""
    history = load_history(repo_root)
    cutoff = time.time() - 86400
    return sum(1 for r in history if r.timestamp > cutoff)


def summarize_history(history: List[EvaluationRecord]) -> str:
    """Produce an LLM-consumable summary of past improvement attempts."""
    if not history:
        return "No previous improvement attempts."

    recent = history[-10:]
    lines = ["# Recent Improvement History\n"]
    for r in recent:
        delta = ""
        if r.test_delta:
            before = r.test_delta.get("before", {})
            after = r.test_delta.get("after", {})
            delta = f" (tests: {before.get('passed', 0)}p/{before.get('failed', 0)}f -> {after.get('passed', 0)}p/{after.get('failed', 0)}f)"
        lines.append(
            f"- [{r.outcome}] {r.task_type}: {r.description}{delta}"
        )
        if r.feedback:
            lines.append(f"  Feedback: {r.feedback}")

    return "\n".join(lines)
