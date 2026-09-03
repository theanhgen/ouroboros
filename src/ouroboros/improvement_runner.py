"""Scheduled self-improvement runner with isolated state and retry logic."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from . import backends, git_ops, llm
from .codebase import get_repo_root
from .config import SafetyConfig, reviewer_safety_kwargs
from .evaluation import check_pr_outcomes
from .improvement import ImprovementResult, ImprovementTask, run_improvement_cycle
from .moltbook import load_runner_config, _send_telegram_message
from .storage import save_json_file

log = logging.getLogger(__name__)


def _send_notification(cfg: Any, message: str, *, is_error: bool = False) -> None:
    """Send a Telegram notification from the improvement runner."""
    if not getattr(cfg, "enable_telegram_notifications", False):
        return
    token = getattr(cfg, "telegram_bot_token", None)
    chat_id = getattr(cfg, "telegram_chat_id", None)
    if token and chat_id:
        _send_telegram_message(token, chat_id, message)

DIRTY_REPO_RETRY_SECONDS = 1800
OPEN_PR_RETRY_SECONDS = 1800


@dataclass
class ScheduledImprovementRun:
    status: str
    message: str
    pr_url: Optional[str] = None
    issue_url: Optional[str] = None


def _scheduler_state_path() -> str:
    return os.path.expanduser("~/.config/moltbook/self_improvement_state.json")


def load_scheduler_state() -> Dict[str, Any]:
    path = _scheduler_state_path()
    if not os.path.exists(path):
        return {
            "consecutive_failures": 0,
            "last_attempt": None,
            "last_error": None,
            "last_issue_url": None,
            "last_pr_url": None,
            "last_status": None,
            "next_due_ts": None,
        }
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_scheduler_state(state: Dict[str, Any]) -> None:
    save_json_file(_scheduler_state_path(), state, sort_keys=True, indent=2)


def _load_feed_context_state() -> Dict[str, Any]:
    path = os.path.expanduser("~/.config/moltbook/state.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _normal_delay_seconds(cfg: Any) -> int:
    return max(1, int(cfg.improvement_interval_hours)) * 3600


def _retry_delay_seconds(cfg: Any, failure_count: int) -> int:
    base = max(1, int(getattr(cfg, "self_improvement_retry_minutes", 60))) * 60
    exponent = max(0, failure_count - 1)
    return min(base * (2**exponent), _normal_delay_seconds(cfg))


def _next_due_ts(state: Dict[str, Any], cfg: Any) -> Optional[int]:
    next_due = state.get("next_due_ts")
    if next_due is not None:
        return int(next_due)

    last_attempt = state.get("last_attempt")
    if last_attempt is None:
        return None
    return int(last_attempt) + _normal_delay_seconds(cfg)


def _set_idle_state(state: Dict[str, Any], now: int, cfg: Any, status: str, *, pr_url: Optional[str] = None) -> None:
    state["consecutive_failures"] = 0
    state["last_attempt"] = now
    state["last_error"] = None
    state["last_pr_url"] = pr_url
    state["last_status"] = status
    state["next_due_ts"] = now + _normal_delay_seconds(cfg)


def _set_deferred_state(state: Dict[str, Any], now: int, status: str, message: str, delay_seconds: int) -> None:
    state["last_attempt"] = now
    state["last_error"] = message
    state["last_status"] = status
    state["next_due_ts"] = now + delay_seconds


def _set_failure_state(
    state: Dict[str, Any],
    now: int,
    cfg: Any,
    status: str,
    error: str,
    *,
    issue_url: Optional[str] = None,
) -> None:
    failures = int(state.get("consecutive_failures", 0)) + 1
    state["consecutive_failures"] = failures
    state["last_attempt"] = now
    state["last_error"] = error
    state["last_issue_url"] = issue_url
    state["last_status"] = status
    state["next_due_ts"] = now + _retry_delay_seconds(cfg, failures)


def _task_issue_marker(task: ImprovementTask) -> str:
    return git_ops.make_auto_issue_marker(
        task.task_type,
        task.description,
        task.target_files,
    )


def _build_followup_issue_body(task: ImprovementTask, result: ImprovementResult, marker: str) -> str:
    lines = [
        "## Autonomous Self-Improvement Blocked",
        "",
        f"**Task type**: {task.task_type}",
        f"**Description**: {task.description}",
        "",
        "### Why this needs attention",
        task.evidence,
        "",
        "### Why the autonomous patch did not complete",
        result.details or result.status,
        "",
        "### Candidate files",
    ]
    for file_path in task.target_files:
        lines.append(f"- `{file_path}`")

    if result.test_before:
        lines.extend(
            [
                "",
                "### Baseline test state",
                f"- {result.test_before.summary()}",
            ]
        )

    if result.test_after:
        lines.extend(
            [
                "",
                "### Validation test state",
                f"- {result.test_after.summary()}",
            ]
        )

    lines.extend(
        [
            "",
            "---",
            "Opened automatically so the problem stays tracked even when the agent cannot publish a safe PR.",
            marker,
        ]
    )
    return "\n".join(lines)


def _maybe_create_followup_issue(repo_root: Any, cfg: Any, result: ImprovementResult) -> Optional[str]:
    if not getattr(cfg, "enable_auto_issue_creation", True):
        return None
    if result.status not in ("failed", "reverted"):
        return None
    if not result.task:
        return None

    marker = _task_issue_marker(result.task)
    existing_url = git_ops.find_open_issue_by_marker(repo_root, marker)
    if existing_url:
        return existing_url

    title = f"[ouroboros] follow-up: {result.task.task_type} - {result.task.description[:70]}"
    body = _build_followup_issue_body(result.task, result, marker)
    try:
        return git_ops.create_issue(repo_root, title, body)
    except Exception:
        log.exception("Failed to create follow-up issue for blocked improvement")
        return None


def run_scheduled_self_improvement(
    *,
    force: bool = False,
    dry_run: bool = False,
    model: Optional[str] = None,
) -> ScheduledImprovementRun:
    cfg = load_runner_config()
    if not cfg.enable_self_improvement:
        return ScheduledImprovementRun("skipped_disabled", "Self-improvement is disabled in runner config.")

    state = load_scheduler_state()
    now = int(time.time())
    next_due = _next_due_ts(state, cfg)
    if not force and next_due is not None and now < next_due:
        return ScheduledImprovementRun(
            "skipped_due",
            f"Next self-improvement run is scheduled for {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_due))}.",
        )

    repo_root = get_repo_root()

    # Auto-commit volatile state files so the worktree is clean for the PR branch.
    try:
        if git_ops.commit_auto_state(repo_root):
            log.info("Auto-committed state files to unblock improvement cycle.")
    except Exception as exc:
        log.warning("Failed to auto-commit state files: %s", exc)

    if not git_ops.is_clean(repo_root):
        _set_deferred_state(
            state,
            now,
            "skipped_dirty_repo",
            "Repository has uncommitted changes; refusing to create autonomous PRs from a dirty worktree.",
            DIRTY_REPO_RETRY_SECONDS,
        )
        save_scheduler_state(state)
        return ScheduledImprovementRun("skipped_dirty_repo", state["last_error"])

    check_pr_outcomes(repo_root)
    open_prs = git_ops.has_open_improvement_prs(repo_root)
    if open_prs is not False:
        # None means the lookup failed; defer rather than risk a second PR for
        # work already in flight.
        _set_deferred_state(
            state,
            now,
            "skipped_open_pr",
            (
                "Open autonomous improvement PR already exists."
                if open_prs
                else "Could not determine whether an improvement PR is open."
            ),
            OPEN_PR_RETRY_SECONDS,
        )
        save_scheduler_state(state)
        return ScheduledImprovementRun("skipped_open_pr", state["last_error"])

    # Build the base client. When the identify role uses a local CLI agent we
    # don't need an OpenAI key at all; only fall back to the OpenAI SDK if a
    # role still requires it (or the CLI binary is unavailable).
    identify_backend = getattr(cfg, "identify_backend", "openai")
    client = None
    if backends.is_cli_backend(identify_backend):
        client = backends.make_backend_client(identify_backend, openai_client=None)
    if client is None:
        client = llm.make_client(llm.load_openai_key())
    combined_state = _load_feed_context_state()
    combined_state["self_improvement_scheduler"] = dict(state)

    safety = SafetyConfig(
        enable_auto_merge=getattr(cfg, "enable_auto_merge", False),
        max_improvements_per_day=getattr(cfg, "max_improvements_per_day", 3),
        identify_backend=getattr(cfg, "identify_backend", "openai"),
        plan_backend=getattr(cfg, "plan_backend", "openai"),
        generator_backend=getattr(cfg, "generator_backend", "openai"),
        generator_model=getattr(cfg, "generator_model", "") or None,
        **reviewer_safety_kwargs(cfg),
    )

    # Build notification callback for Telegram
    def _on_event(event_type: str, message: str, data: dict) -> None:
        if event_type in (
            "task_identified", "pr_created", "auto_merged",
            "reverted", "baseline_broken", "retry_success",
        ):
            _send_notification(cfg, message)
        elif event_type == "failed":
            _send_notification(cfg, message, is_error=True)

    try:
        result = run_improvement_cycle(
            client,
            combined_state,
            safety,
            model=model or cfg.improvement_model,
            dry_run=dry_run,
            on_event=_on_event,
        )
    except Exception as exc:
        log.exception("Scheduled self-improvement run failed")
        _set_failure_state(state, now, cfg, "failed", f"Unhandled scheduler exception: {exc}")
        _send_notification(cfg, f"Scheduled improvement failed: {exc}", is_error=True)
        save_scheduler_state(state)
        return ScheduledImprovementRun("failed", state["last_error"])

    if dry_run:
        if result is None:
            return ScheduledImprovementRun("dry_run", "No improvements identified.")
        return ScheduledImprovementRun("dry_run", f"Would attempt: {result.task.description}")

    if result is None:
        _set_idle_state(state, now, cfg, "idle")
        save_scheduler_state(state)
        return ScheduledImprovementRun("idle", "No improvements identified.")

    if result.status == "success":
        _set_idle_state(state, now, cfg, "success", pr_url=result.pr_url)
        state["last_issue_url"] = None
        save_scheduler_state(state)
        # PR created/merged notifications handled by on_event callback
        return ScheduledImprovementRun(
            "success",
            result.task.description,
            pr_url=result.pr_url,
        )

    issue_url = _maybe_create_followup_issue(repo_root, cfg, result)
    if issue_url:
        _send_notification(
            cfg,
            f"Created follow-up issue for blocked improvement: {result.task.description[:80]}\n{issue_url}",
        )
    _set_failure_state(
        state,
        now,
        cfg,
        result.status,
        result.details or f"Self-improvement ended with status {result.status}.",
        issue_url=issue_url,
    )
    save_scheduler_state(state)
    return ScheduledImprovementRun(
        result.status,
        result.details or result.task.description,
        issue_url=issue_url,
    )
