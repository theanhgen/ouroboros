"""Outcome tracking for self-improvement attempts."""

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import git_ops
from .codebase import get_repo_root
from .model_defaults import DEFAULT_OPENAI_MODEL
from .storage import (
    CycleRecord,
    MetricRecord,
    OuroborosStorage,
    load_json_file,
    save_json_file,
)

log = logging.getLogger(__name__)

# Approximate pricing per 1M tokens (input, output).
MODEL_PRICING: Dict[str, Tuple[float, float]] = {
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4-nano-2026-03-17": (0.20, 1.25),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-3.5-turbo": (0.50, 1.50),
}
_DEFAULT_PRICING = (2.50, 10.00)

HISTORY_FILE = "config/improvement_history.json"
TERMINAL_OUTCOMES = frozenset({"closed", "failed", "merged", "reverted", "skipped"})


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


def record_improvement(result: "ImprovementResult", repo_root: Optional[Path] = None, model: str = DEFAULT_OPENAI_MODEL) -> None:
    """Append an improvement result to the history file and SQLite storage."""
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
                "errors": result.test_before.errors if result.test_before else 0,
            },
            "after": {
                "passed": result.test_after.passed if result.test_after else 0,
                "failed": result.test_after.failed if result.test_after else 0,
                "errors": result.test_after.errors if result.test_after else 0,
            },
        },
        pr_url=result.pr_url or "",
        outcome=result.status,
        feedback=result.details or "",
        timestamp=time.time(),
    )
    history.append(record)

    save_json_file(path, [r.to_dict() for r in history])

    # Persist to SQLite
    try:
        storage = OuroborosStorage()
        cycle_id = storage.record_cycle(CycleRecord(
            ts=record.timestamp,
            task_type=record.task_type,
            model=model,
            status=record.outcome,
            description=record.description,
        ))
        
        prompt_tokens = result.total_usage.get("prompt_tokens", 0)
        completion_tokens = result.total_usage.get("completion_tokens", 0)
        input_rate, output_rate = MODEL_PRICING.get(model, _DEFAULT_PRICING)
        cost = (prompt_tokens / 1_000_000 * input_rate) + (completion_tokens / 1_000_000 * output_rate)
        
        storage.record_metrics(MetricRecord(
            cycle_id=cycle_id,
            tokens_in=prompt_tokens,
            tokens_out=completion_tokens,
            cost=cost
        ))
    except Exception:
        log.exception("Failed to persist metrics to SQLite")


def load_history(repo_root: Optional[Path] = None) -> List[EvaluationRecord]:
    """Load improvement history from disk."""
    data = load_json_file(
        _history_path(repo_root),
        default=[],
        error_msg="Corrupt improvement history file, returning empty",
        logger=log,
    )
    if not isinstance(data, list):
        log.warning("Improvement history is not a list, returning empty")
        return []
    return [EvaluationRecord.from_dict(d) for d in data]


def check_pr_outcomes(repo_root: Optional[Path] = None) -> List[EvaluationRecord]:
    """Poll open PRs and update their outcomes in history."""
    root = repo_root or get_repo_root()
    history = load_history(root)
    updated = False

    for record in history:
        if not record.pr_url or record.outcome in TERMINAL_OUTCOMES:
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
                feedback = git_ops.get_pr_feedback(root, record.pr_url)
                if feedback is None:
                    # Marking the record terminal now would stop it ever being
                    # polled again, permanently losing the review text. Leave
                    # it open and pick it up on the next pass.
                    log.info(
                        "PR %s is %s but its feedback could not be fetched; "
                        "leaving it open to retry",
                        record.pr_url,
                        state.lower(),
                    )
                    continue
                record.outcome = state.lower()
                record.feedback = feedback
                updated = True
                log.info("PR %s outcome updated: %s", record.pr_url, record.outcome)
        except Exception:
            log.debug("Could not check PR status for %s", record.pr_url)

    if updated:
        save_json_file(_history_path(root), [r.to_dict() for r in history])

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
