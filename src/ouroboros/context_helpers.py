"""Prompt-context builders shared by the improvement cycle."""

from typing import List

from .evaluation import EvaluationRecord


def _build_failed_attempts_context(history: List[EvaluationRecord], max_entries: int = 5) -> str:
    """Format recent failed/reverted attempts as negative examples for the LLM."""
    failed = [
        r for r in history
        if r.outcome in ("closed", "failed", "reverted")
    ]
    if not failed:
        return ""
    recent = failed[-max_entries:]
    lines = ["### Previously Failed Attempts (DO NOT repeat these)"]
    for r in recent:
        line = f"- [{r.task_type}] {r.description}"
        if r.feedback:
            line += f" -- feedback: {r.feedback[:120]}"
        lines.append(line)
    return "\n".join(lines)
