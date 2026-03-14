"""Core self-improvement engine -- identify, plan, generate, validate, PR."""

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import git_ops, llm
from .codebase import get_codebase_summary, get_repo_root, read_file_raw
from .config import SafetyConfig
from .evaluation import (
    EvaluationRecord,
    improvements_today,
    load_history,
    record_improvement,
    summarize_history,
)
from .test_runner import TestResult, run_tests

log = logging.getLogger(__name__)

# Type alias for the notification callback.
# on_event(event_type: str, message: str, data: dict)
EventCallback = Optional[Callable[[str, str, Dict[str, Any]], None]]


@dataclass
class ImprovementTask:
    task_id: str
    task_type: str  # fix_test | add_test | fix_bug | refactor | improve_docs | add_feature
    description: str
    target_files: List[str]
    evidence: str

    @classmethod
    def from_llm_response(cls, data: dict) -> "ImprovementTask":
        return cls(
            task_id=str(uuid.uuid4())[:8],
            task_type=data.get("task_type", "fix_bug"),
            description=data.get("description", ""),
            target_files=data.get("target_files", []),
            evidence=data.get("evidence", ""),
        )


@dataclass
class CodeChange:
    file_path: str
    original_content: str
    new_content: str
    description: str


@dataclass
class ImprovementResult:
    task: ImprovementTask
    changes: List[CodeChange] = field(default_factory=list)
    test_before: Optional[TestResult] = None
    test_after: Optional[TestResult] = None
    pr_url: Optional[str] = None
    details: str = ""
    status: str = "pending"  # pending | success | failed | reverted | skipped


# Hardcoded immutable files that can never be modified
IMMUTABLE_FILES = frozenset({
    "config.py",
    "improvement.py",
    "git_ops.py",
    "evaluation.py",
    "policies.py",
})


def _is_path_allowed(file_path: str, config: SafetyConfig) -> bool:
    """Check if a file path is allowed for modification."""
    # Check forbidden paths (match by filename)
    basename = Path(file_path).name
    if basename in IMMUTABLE_FILES:
        return False
    if basename in config.forbidden_modification_paths:
        return False

    # Check allowed paths (match by prefix)
    for allowed in config.allowed_modification_paths:
        if file_path.startswith(allowed):
            return True

    return False


def _validate_changes(changes: List[CodeChange], config: SafetyConfig) -> List[str]:
    """Validate that proposed changes respect safety constraints.

    Returns a list of violation messages (empty = valid).
    """
    violations = []

    if len(changes) > config.max_changed_files_per_pr:
        violations.append(
            f"Too many files changed: {len(changes)} > {config.max_changed_files_per_pr}"
        )

    total_lines = 0
    for change in changes:
        if not _is_path_allowed(change.file_path, config):
            violations.append(f"Forbidden file modification: {change.file_path}")

        orig_lines = change.original_content.count("\n")
        new_lines = change.new_content.count("\n")
        total_lines += abs(new_lines - orig_lines) + _count_changed_lines(
            change.original_content, change.new_content
        )

    if total_lines > config.max_lines_changed_per_pr:
        violations.append(
            f"Too many lines changed: {total_lines} > {config.max_lines_changed_per_pr}"
        )

    return violations


def _count_changed_lines(original: str, new: str) -> int:
    """Count the number of lines that differ between two strings."""
    orig_lines = original.splitlines()
    new_lines = new.splitlines()
    # Simple diff count: lines added + lines removed
    max_len = max(len(orig_lines), len(new_lines))
    changed = 0
    for i in range(max_len):
        orig = orig_lines[i] if i < len(orig_lines) else None
        new = new_lines[i] if i < len(new_lines) else None
        if orig != new:
            changed += 1
    return changed


def identify_improvements(
    client: Any,
    codebase_summary: str,
    test_results: TestResult,
    history: List[EvaluationRecord],
    model: str = "gpt-4o",
    additional_context: str = "",
) -> Optional[ImprovementTask]:
    """Ask the LLM to identify one improvement to make."""
    test_summary = test_results.summary()
    if test_results.failure_details:
        test_summary += "\n\nFailure details:\n"
        for fail in test_results.failure_details:
            test_summary += f"- {fail.file}::{fail.test_name}: {fail.message}\n"
            if fail.traceback:
                test_summary += f"  {fail.traceback[:200]}\n"

    history_summary = summarize_history(history)
    result = llm.analyze_codebase(
        client, codebase_summary, test_summary, history_summary,
        model=model, additional_context=additional_context,
    )

    if not result or result.get("task_type") == "none":
        return None

    return ImprovementTask.from_llm_response(result)


def plan_improvement(
    client: Any,
    task: ImprovementTask,
    relevant_code: Dict[str, str],
    model: str = "gpt-4o",
) -> Optional[str]:
    """Generate a plan for the improvement."""
    code_text = "\n\n".join(
        f"### {path}\n{content}" for path, content in relevant_code.items()
    )
    task_dict = {
        "task_type": task.task_type,
        "description": task.description,
        "target_files": task.target_files,
        "evidence": task.evidence,
    }
    return llm.plan_code_change(client, task_dict, code_text, model=model)


def generate_changes(
    client: Any,
    task: ImprovementTask,
    plan: str,
    file_contents: Dict[str, str],
    config: SafetyConfig,
    model: str = "gpt-4o",
) -> Optional[List[CodeChange]]:
    """Generate code changes from a plan."""
    constraints = (
        f"- Maximum {config.max_changed_files_per_pr} files\n"
        f"- Maximum {config.max_lines_changed_per_pr} lines changed\n"
        f"- Only modify files under: {', '.join(config.allowed_modification_paths)}\n"
        f"- NEVER modify: {', '.join(config.forbidden_modification_paths)}\n"
        f"- Task type: {task.task_type}"
    )

    raw_changes = llm.generate_code(client, plan, file_contents, constraints, model=model)
    if not raw_changes:
        return None

    changes = []
    for raw in raw_changes:
        file_path = raw.get("file_path", "")
        new_content = raw.get("new_content", "")
        description = raw.get("description", "")
        original = file_contents.get(file_path, "")
        changes.append(CodeChange(
            file_path=file_path,
            original_content=original,
            new_content=new_content,
            description=description,
        ))

    return changes


def apply_changes(changes: List[CodeChange], repo_root: Path) -> None:
    """Write changes to disk. Raises on forbidden paths."""
    config = SafetyConfig()
    for change in changes:
        if not _is_path_allowed(change.file_path, config):
            raise PermissionError(f"Cannot modify forbidden file: {change.file_path}")

        full_path = repo_root / change.file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(change.new_content, encoding="utf-8")


def revert_changes(changes: List[CodeChange], repo_root: Path) -> None:
    """Revert changes by restoring original file contents."""
    for change in changes:
        full_path = repo_root / change.file_path
        if change.original_content:
            full_path.write_text(change.original_content, encoding="utf-8")
        elif full_path.exists():
            # File was newly created, remove it
            full_path.unlink()


def _format_failure_details(test_result: TestResult) -> str:
    """Format test failure details for LLM root cause analysis."""
    if not test_result.failure_details:
        return f"Tests: {test_result.summary()}"
    lines = [f"Tests: {test_result.summary()}", "", "Failure details:"]
    for fail in test_result.failure_details:
        lines.append(f"- {fail.file}::{fail.test_name}: {fail.message}")
        if fail.traceback:
            lines.append(f"  Traceback: {fail.traceback[:500]}")
    return "\n".join(lines)


def _retry_with_root_cause(
    client: Any,
    task: ImprovementTask,
    original_changes: List[CodeChange],
    test_before: TestResult,
    test_after: TestResult,
    config: SafetyConfig,
    repo_root: Path,
    model: str = "gpt-4o",
    on_event: EventCallback = None,
) -> Optional[ImprovementResult]:
    """After a test regression, analyze the root cause and retry once."""
    if on_event:
        on_event("retry_start", "Analyzing failure root cause for retry...", {
            "task_type": task.task_type,
        })

    failure_info = _format_failure_details(test_after)
    original_code = {c.file_path: c.original_content for c in original_changes}
    attempted_code = {c.file_path: c.new_content for c in original_changes}

    retry_prompt = (
        f"The previous code change caused test regressions.\n\n"
        f"## Task\n{task.description}\n\n"
        f"## Test results BEFORE change\n{test_before.summary()}\n\n"
        f"## Test results AFTER change (REGRESSION)\n{failure_info}\n\n"
        f"## What was attempted\n"
    )
    for fp, content in attempted_code.items():
        retry_prompt += f"\n### {fp} (attempted version)\n```\n{content[:2000]}\n```\n"

    retry_prompt += (
        "\n\nAnalyze why the tests failed and generate a CORRECTED version "
        "that fixes the original task WITHOUT causing test regressions."
    )

    constraints = (
        f"- Maximum {config.max_changed_files_per_pr} files\n"
        f"- Maximum {config.max_lines_changed_per_pr} lines changed\n"
        f"- Only modify files under: {', '.join(config.allowed_modification_paths)}\n"
        f"- NEVER modify: {', '.join(config.forbidden_modification_paths)}\n"
        f"- Task type: {task.task_type}"
    )

    raw_changes = llm.generate_code(client, retry_prompt, original_code, constraints, model=model)
    if not raw_changes:
        log.info("[retry] LLM could not generate corrected code")
        return None

    retry_changes = []
    for raw in raw_changes:
        file_path = raw.get("file_path", "")
        new_content = raw.get("new_content", "")
        description = raw.get("description", "")
        original = original_code.get(file_path, "")
        retry_changes.append(CodeChange(
            file_path=file_path,
            original_content=original,
            new_content=new_content,
            description=description,
        ))

    violations = _validate_changes(retry_changes, config)
    if violations:
        log.info("[retry] Corrected code has safety violations: %s", violations)
        return None

    try:
        apply_changes(retry_changes, repo_root)
    except PermissionError:
        return None

    retry_test = run_tests(repo_root)
    log.info("[retry] Tests after corrected code: %s", retry_test.summary())

    if retry_test.failed > test_before.failed or retry_test.errors > test_before.errors:
        log.warning("[retry] Corrected code still regresses, reverting")
        revert_changes(retry_changes, repo_root)
        return None

    if on_event:
        on_event("retry_success", "Retry succeeded -- corrected code passes tests", {
            "tests_passed": retry_test.passed,
        })

    result = ImprovementResult(
        task=task,
        changes=retry_changes,
        test_before=test_before,
        test_after=retry_test,
        status="success",
        details="Succeeded on retry after root cause analysis",
    )
    return result


def validate_improvement(
    task: ImprovementTask,
    changes: List[CodeChange],
    repo_root: Path,
    *,
    client: Any = None,
    config: SafetyConfig | None = None,
    model: str = "gpt-4o",
    on_event: EventCallback = None,
) -> ImprovementResult:
    """Apply changes, run tests, revert if tests regress.

    If tests regress and client is provided with config.max_retry_on_failure > 0,
    performs root cause analysis and retries once.

    Returns an ImprovementResult with test_before, test_after, and status.
    """
    config = config or SafetyConfig()
    result = ImprovementResult(task=task, changes=changes)

    # Run tests before changes
    result.test_before = run_tests(repo_root)
    log.info("Tests before: %s", result.test_before.summary())

    # Validate safety constraints
    violations = _validate_changes(changes, config)
    if violations:
        log.warning("Safety violations: %s", violations)
        result.details = "; ".join(violations)
        result.status = "failed"
        return result

    # Apply changes
    try:
        apply_changes(changes, repo_root)
    except PermissionError as e:
        log.error("Permission denied: %s", e)
        result.details = str(e)
        result.status = "failed"
        return result

    # Run tests after changes
    result.test_after = run_tests(repo_root)
    log.info("Tests after: %s", result.test_after.summary())

    # Check for regression
    has_regression = (
        result.test_after.failed > result.test_before.failed
        or result.test_after.errors > result.test_before.errors
    )

    if has_regression:
        regression_type = "failures" if result.test_after.failed > result.test_before.failed else "errors"
        log.warning(
            "Test regression detected (%s), reverting",
            regression_type,
        )
        revert_changes(changes, repo_root)

        # Attempt retry with root cause analysis
        if client and config.max_retry_on_failure > 0:
            log.info("[retry] Attempting root cause analysis and retry...")
            retry_result = _retry_with_root_cause(
                client, task, changes,
                result.test_before, result.test_after,
                config, repo_root, model=model,
                on_event=on_event,
            )
            if retry_result:
                return retry_result
            log.info("[retry] Retry failed, keeping revert")

        if result.test_after.failed > result.test_before.failed:
            result.details = (
                f"Test regression detected: {result.test_before.failed} failures before, "
                f"{result.test_after.failed} after"
            )
        else:
            result.details = (
                f"New test errors detected: {result.test_before.errors} errors before, "
                f"{result.test_after.errors} after"
            )
        result.status = "reverted"
        return result

    result.status = "success"
    return result


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


def _build_success_rate_context(history: List[EvaluationRecord]) -> str:
    """Compute per-task_type success rates and format as LLM context."""
    from collections import Counter
    attempts: Counter = Counter()
    successes: Counter = Counter()
    for r in history:
        attempts[r.task_type] += 1
        if r.outcome in ("merged", "success"):
            successes[r.task_type] += 1
    if not attempts:
        return ""
    lines = ["### Task-Type Success Rates (prefer higher rates)"]
    for tt, total in attempts.most_common():
        wins = successes.get(tt, 0)
        pct = int(100 * wins / total)
        lines.append(f"- {tt}: {wins}/{total} ({pct}%)")
    return "\n".join(lines)


def _assemble_feed_context(client: Any, state: Dict[str, Any]) -> str:
    """Build additional context string from feed intelligence state keys."""
    parts = []

    # Comment mining suggestions
    suggestions = state.get("feed_improvement_suggestions", [])
    if suggestions:
        lines = ["### Feed Improvement Suggestions"]
        for s in suggestions[-10:]:  # last 10
            lines.append(f"- [{s.get('post_title', '')}] {s.get('insight', '')}")
        parts.append("\n".join(lines))

    # Engagement scores / topic signals
    scores = state.get("engagement_scores", [])
    if scores:
        lines = [
            "### Community Engagement Signals",
            "HIGH-ENGAGEMENT topics below should be STRONGLY PREFERRED when choosing improvements.",
            "Topics with more replies and upvotes indicate real community interest.",
        ]
        for s in sorted(
            scores,
            key=lambda x: x.get("reply_count", 0) + x.get("upvotes", 0),
            reverse=True,
        )[:10]:
            votes = f"+{s.get('upvotes', 0)}/-{s.get('downvotes', 0)}"
            lines.append(
                f"- {s.get('post_title', '')} ({s.get('reply_count', 0)} replies, {votes}): "
                f"{s.get('topic_signal', '')}"
            )
        parts.append("\n".join(lines))

    # Knowledge base summary
    try:
        from .knowledge_base import load_kb, get_summary
        kb = load_kb()
        if kb.get("entries"):
            summary = get_summary(client, kb=kb)
            if summary:
                parts.append(f"### Knowledge Base Summary\n{summary}")
    except Exception:
        log.debug("Knowledge base not available for context assembly")

    return "\n\n".join(parts)


def run_improvement_cycle(
    client: Any,
    state: Dict[str, Any],
    config: SafetyConfig | None = None,
    model: str = "gpt-4o",
    dry_run: bool = False,
    on_event: EventCallback = None,
) -> Optional[ImprovementResult]:
    """Run a full improvement cycle: identify -> plan -> generate -> validate -> PR.

    Args:
        on_event: Optional callback fired at each stage.
            Signature: on_event(event_type, message, data_dict)
            Event types: cycle_start, task_identified, baseline_broken,
            planning, generating, validating, pr_created, auto_merged,
            reverted, failed, cycle_end.

    Returns ImprovementResult or None if no improvement was identified/attempted.
    """
    config = config or SafetyConfig()
    repo_root = get_repo_root()

    def _fire(event_type: str, message: str, data: Dict[str, Any] | None = None) -> None:
        if on_event:
            on_event(event_type, message, data or {})

    _fire("cycle_start", "Starting improvement cycle")

    # Rate limiting
    today_count = improvements_today(repo_root)
    if today_count >= config.max_improvements_per_day:
        log.info("Rate limit reached: %d improvements today (max %d)", today_count, config.max_improvements_per_day)
        _fire("cycle_end", f"Rate limit reached: {today_count}/{config.max_improvements_per_day} today")
        return None

    # Check for open PRs
    if git_ops.has_open_improvement_prs(repo_root):
        log.info("Skipping improvement: open improvement PRs exist")
        _fire("cycle_end", "Skipped: open improvement PRs exist")
        return None

    # Dedup: skip if recent attempts at the same task_type made no test progress
    history = load_history(repo_root)
    _MAX_STALE_ATTEMPTS = 2
    recent = [r for r in history if r.timestamp > time.time() - 7 * 86400]
    if recent:
        # Group consecutive latest attempts by task_type
        last_type = recent[-1].task_type
        streak = [r for r in reversed(recent) if r.task_type == last_type]
        if len(streak) >= _MAX_STALE_ATTEMPTS:
            all_no_progress = all(
                r.test_delta.get("before") == r.test_delta.get("after")
                for r in streak[:_MAX_STALE_ATTEMPTS]
            )
            if all_no_progress:
                log.info(
                    "Skipping improvement: %d recent '%s' attempts with no test progress",
                    len(streak), last_type,
                )
                _fire("cycle_end", f"Skipped: {len(streak)} stale '{last_type}' attempts")
                return None

    # Feedback-aware staleness: skip task_type if most recent closed PR got negative feedback
    _NEGATIVE_KEYWORDS = (
        "wrong approach", "not needed", "revert", "do not merge",
        "nack", "reject", "bad idea", "unnecessary", "broke",
    )
    closed_with_feedback = [
        r for r in recent
        if r.outcome in ("closed", "merged") and r.feedback
    ]
    if closed_with_feedback:
        last_fb = closed_with_feedback[-1]
        fb_lower = last_fb.feedback.lower()
        if any(kw in fb_lower for kw in _NEGATIVE_KEYWORDS):
            log.info(
                "Skipping improvement: negative feedback on recent '%s' PR (%s)",
                last_fb.task_type, last_fb.pr_url,
            )
            _fire("cycle_end", f"Skipped: negative feedback on recent '{last_fb.task_type}' PR")
            return None

    # Step 1: Understand the codebase
    log.info("[improve] Analyzing codebase...")
    codebase_summary = get_codebase_summary(repo_root)
    test_results = run_tests(repo_root)

    # Broken baseline detection: if tests are failing before any changes,
    # force the LLM to prioritize fix_test with the failure details
    baseline_broken = test_results.failed > 0 or test_results.errors > 0
    baseline_context = ""
    if baseline_broken:
        log.warning(
            "[improve] Broken baseline detected: %d failed, %d errors",
            test_results.failed, test_results.errors,
        )
        _fire("baseline_broken", f"Baseline broken: {test_results.failed} failed, {test_results.errors} errors", {
            "failed": test_results.failed,
            "errors": test_results.errors,
        })
        baseline_context = (
            "\n\n### CRITICAL: Broken Baseline\n"
            "Tests are ALREADY FAILING before any changes. "
            "You MUST prioritize task_type='fix_test' to fix the existing failures.\n"
            f"Current test state: {test_results.summary()}\n"
        )
        if test_results.failure_details:
            baseline_context += "\nFailing tests:\n"
            for fail in test_results.failure_details:
                baseline_context += f"- {fail.file}::{fail.test_name}: {fail.message}\n"
                if fail.traceback:
                    baseline_context += f"  {fail.traceback[:300]}\n"

    # Assemble additional context from feed intelligence
    additional_context = _assemble_feed_context(client, state)

    # Inject broken baseline context (highest priority)
    if baseline_context:
        additional_context = f"{baseline_context}\n\n{additional_context}" if additional_context else baseline_context

    # Append success-rate signal so the LLM prefers task types with higher win rates
    sr_context = _build_success_rate_context(history)
    if sr_context:
        additional_context = f"{additional_context}\n\n{sr_context}" if additional_context else sr_context

    # Append failed attempts so the LLM avoids repeating past failures
    fa_context = _build_failed_attempts_context(history)
    if fa_context:
        additional_context = f"{additional_context}\n\n{fa_context}" if additional_context else fa_context

    # Append backlog items if available
    try:
        from .backlog import load_backlog, format_backlog_for_llm
        backlog = load_backlog(repo_root)
        bl_context = format_backlog_for_llm(backlog)
        if bl_context:
            additional_context = f"{additional_context}\n\n{bl_context}" if additional_context else bl_context
    except Exception:
        log.debug("Backlog not available")

    # Step 2: Identify an improvement
    log.info("[improve] Identifying improvements...")
    task = identify_improvements(
        client, codebase_summary, test_results, history,
        model=model, additional_context=additional_context,
    )
    if not task:
        log.info("[improve] No improvements identified")
        _fire("cycle_end", "No improvements identified")
        return None

    log.info("[improve] Identified: [%s] %s", task.task_type, task.description)
    _fire("task_identified", f"Task: [{task.task_type}] {task.description}", {
        "task_type": task.task_type,
        "description": task.description,
        "target_files": task.target_files,
    })

    if dry_run:
        result = ImprovementResult(task=task, status="skipped")
        log.info("[improve] Dry run -- would proceed with: %s", task.description)
        return result

    # Step 3: Read target files
    relevant_code = {}
    for file_path in task.target_files:
        full_path = repo_root / file_path
        if full_path.exists():
            relevant_code[file_path] = read_file_raw(full_path)
        else:
            relevant_code[file_path] = ""

    # Step 4: Plan the improvement
    log.info("[improve] Planning changes...")
    _fire("planning", f"Planning: {task.description[:100]}")
    plan = plan_improvement(client, task, relevant_code, model=model)
    if not plan:
        log.warning("[improve] Failed to generate plan")
        improvement_result = ImprovementResult(
            task=task,
            test_before=test_results,
            status="failed",
            details="Failed to generate a concrete implementation plan",
        )
        _fire("failed", f"Failed to plan: {task.description[:100]}")
        record_improvement(improvement_result, repo_root)
        return improvement_result

    # Step 5: Generate code changes
    log.info("[improve] Generating code changes...")
    _fire("generating", f"Generating code for: {task.description[:100]}")
    changes = generate_changes(client, task, plan, relevant_code, config, model=model)
    if not changes:
        log.warning("[improve] Failed to generate code changes")
        improvement_result = ImprovementResult(
            task=task,
            test_before=test_results,
            status="failed",
            details="Failed to generate code changes from the approved plan",
        )
        _fire("failed", f"Failed to generate code: {task.description[:100]}")
        record_improvement(improvement_result, repo_root)
        return improvement_result

    # Step 6: Validate (apply, test, revert if needed, retry on regression)
    log.info("[improve] Validating changes...")
    _fire("validating", f"Validating: {task.description[:100]}")
    improvement_result = validate_improvement(
        task, changes, repo_root,
        client=client, config=config, model=model,
        on_event=on_event,
    )

    if improvement_result.status == "reverted":
        _fire("reverted", (
            f"Reverted: {task.description[:80]}\n"
            f"{improvement_result.details}"
        ), {
            "before_passed": improvement_result.test_before.passed if improvement_result.test_before else 0,
            "after_passed": improvement_result.test_after.passed if improvement_result.test_after else 0,
        })

    if improvement_result.status != "success":
        log.warning("[improve] Improvement failed: %s", improvement_result.status)
        record_improvement(improvement_result, repo_root)
        return improvement_result

    # Step 7: Create PR
    log.info("[improve] Creating PR...")
    branch_name = git_ops.make_branch_name(task.task_type)
    original_branch = git_ops.current_branch(repo_root)

    try:
        git_ops.create_branch(repo_root, branch_name)
        changed_files = [c.file_path for c in improvement_result.changes]
        commit_msg = f"ouroboros: {task.task_type} - {task.description}"
        git_ops.commit_changes(repo_root, commit_msg, changed_files)
        git_ops.push_branch(repo_root, branch_name)

        pr_body = _build_pr_body(task, improvement_result.changes, improvement_result)
        pr_url = git_ops.create_pr(
            repo_root,
            title=f"[ouroboros] {task.task_type}: {task.description[:60]}",
            body=pr_body,
            base="main",
        )
        improvement_result.pr_url = pr_url
        log.info("[improve] PR created: %s", pr_url)
        _fire("pr_created", f"PR created: {task.description[:80]}\n{pr_url}", {
            "pr_url": pr_url,
            "task_type": task.task_type,
            "tests_passed": improvement_result.test_after.passed if improvement_result.test_after else 0,
        })

        # Step 8: Auto-merge if enabled
        if config.enable_auto_merge and pr_url:
            log.info("[improve] Attempting auto-merge...")
            merged = git_ops.auto_merge_pr(repo_root, pr_url)
            if merged:
                _fire("auto_merged", f"Auto-merged: {task.description[:80]}\n{pr_url}", {
                    "pr_url": pr_url,
                })
            else:
                log.info("[improve] Auto-merge not completed (may be pending checks)")

    except Exception:
        log.exception("[improve] Failed to create PR")
        improvement_result.details = "Validated changes could not be published as a pull request"
        improvement_result.status = "failed"
        _fire("failed", f"Failed to create PR: {task.description[:80]}")
    finally:
        # Return to original branch
        git_ops.checkout_branch(repo_root, original_branch)

    record_improvement(improvement_result, repo_root)

    # Record metrics snapshot
    try:
        from .metrics import record_snapshot
        record_snapshot(repo_root, improvement_result)
    except Exception:
        log.debug("Metrics recording failed")

    _fire("cycle_end", f"Cycle complete: [{improvement_result.status}] {task.description[:80]}", {
        "status": improvement_result.status,
        "pr_url": improvement_result.pr_url,
    })
    return improvement_result


def _build_pr_body(
    task: ImprovementTask,
    changes: List[CodeChange],
    result: ImprovementResult,
) -> str:
    """Build the PR description."""
    lines = [
        "## Autonomous Self-Improvement",
        "",
        f"**Type**: {task.task_type}",
        f"**Task ID**: {task.task_id}",
        "",
        f"### Description",
        task.description,
        "",
        f"### Evidence",
        task.evidence,
        "",
        "### Changes",
    ]

    for change in changes:
        lines.append(f"- `{change.file_path}`: {change.description}")

    lines.extend([
        "",
        "### Test Results",
        f"- **Before**: {result.test_before.summary() if result.test_before else 'N/A'}",
        f"- **After**: {result.test_after.summary() if result.test_after else 'N/A'}",
        "",
        "---",
        "Generated autonomously by Ouroboros self-improvement engine.",
    ])

    return "\n".join(lines)
