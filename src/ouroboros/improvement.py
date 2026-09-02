"""Core self-improvement engine -- identify, plan, generate, validate, PR."""

import datetime
import logging
import time
import uuid
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import backends, git_ops, llm
from .codebase import get_codebase_summary, get_repo_root, read_file_raw
from .config import SafetyConfig
from .evaluation import (
    EvaluationRecord,
    improvements_today,
    load_history,
    record_improvement,
    summarize_history,
)
from .model_defaults import DEFAULT_OPENAI_MODEL
from .test_runner import RunnerOutcome, run_tests

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
    _usage: Optional[Dict[str, int]] = None

    @classmethod
    def from_llm_response(cls, data: dict) -> "ImprovementTask":
        return cls(
            task_id=str(uuid.uuid4())[:8],
            task_type=data.get("task_type", "fix_bug"),
            description=data.get("description", ""),
            target_files=data.get("target_files", []),
            evidence=data.get("evidence", ""),
            _usage=data.get("_usage"),
        )


@dataclass
class CodeChange:
    file_path: str
    original_content: str
    new_content: str
    description: str
    # Whether the file was already on disk before the change was applied.
    # An empty original_content cannot tell a newly created file from an
    # existing empty one, so apply_changes stamps this from the filesystem and
    # revert_changes trusts it instead of guessing (#91). None = never went
    # through apply_changes, so revert_changes leaves that file alone.
    existed_before: Optional[bool] = None


@dataclass
class ImprovementResult:
    task: ImprovementTask
    changes: List[CodeChange] = field(default_factory=list)
    test_before: Optional[RunnerOutcome] = None
    test_after: Optional[RunnerOutcome] = None
    pr_url: Optional[str] = None
    details: str = ""
    status: str = "pending"  # pending | success | failed | reverted | skipped
    total_usage: Dict[str, int] = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})


# Hardcoded immutable files that can never be modified
IMMUTABLE_FILES = frozenset({
    "config.py",
    "improvement.py",
    "git_ops.py",
    "evaluation.py",
    "policies.py",
})


PYTHON_SUFFIXES = frozenset({".py", ".pyi", ".pyw"})


def _is_python_source(file_path: str, content: str = "") -> bool:
    """Whether this change should be read as Python.

    By suffix, or by shebang for an extensionless script. Not "try to parse
    it and see": a Markdown or JSON change is not source, and reporting it as
    unparseable would block legitimate work.
    """
    # Trailing spaces and dots are stripped by some filesystems on write, so
    # "foo.py " can land as "foo.py" -- judge it as the file it will become.
    trimmed = file_path.rstrip(" .")
    if Path(trimmed).suffix.lower() in PYTHON_SUFFIXES:
        return True

    first_line = content.split("\n", 1)[0] if content else ""
    return first_line.startswith("#!") and "python" in first_line.lower()


def _is_path_allowed(file_path: str, config: SafetyConfig) -> bool:
    """Check if a file path is allowed for modification.

    Both halves come from policies, so this gate and
    validate_modification_scope cannot drift apart -- they did, and the raw
    prefix compare here accepted "src/../../etc/passwd" as being under "src/".
    """
    from .policies import is_forbidden_modification_path, is_within_allowed_paths

    if Path(file_path).name in IMMUTABLE_FILES:
        return False
    if is_forbidden_modification_path(file_path, config.forbidden_modification_paths):
        return False

    return is_within_allowed_paths(file_path, config.allowed_modification_paths)


def _validate_changes(changes: List[CodeChange], config: SafetyConfig) -> List[str]:
    """Validate that proposed changes respect safety constraints.

    Returns a list of violation messages (empty = valid).
    """
    from .policies import validate_import_policy

    violations = []

    if len(changes) > config.max_changed_files_per_pr:
        violations.append(
            f"Too many files changed: {len(changes)} > {config.max_changed_files_per_pr}"
        )

    total_lines = 0
    for change in changes:
        if not _is_path_allowed(change.file_path, config):
            violations.append(f"Forbidden file modification: {change.file_path}")

        # Only Python is parseable, and a JSON or Markdown change reported as
        # unparseable would be a false positive, not a finding. Stubs and .pyw
        # count, and the suffix is lowercased because a case-insensitive
        # filesystem makes "x.PY" the same file as "x.py".
        if _is_python_source(change.file_path, change.new_content):
            violations.extend(
                validate_import_policy(
                    change.file_path, change.new_content, config
                )
            )

        total_lines += _count_changed_lines(
            change.original_content, change.new_content
        )

    if total_lines > config.max_lines_changed_per_pr:
        violations.append(
            f"Too many lines changed: {total_lines} > {config.max_lines_changed_per_pr}"
        )

    return violations


def _count_changed_lines(original: str, new: str) -> int:
    """Count added + removed lines using a real line diff.

    Uses difflib so that inserting or deleting lines near the top of a file does
    not mis-align every subsequent line (an index-by-index comparison reports
    almost the whole file as changed for a small early insertion, which would
    trip the line-change cap and wrongly reject correct changes). This matches
    the semantics of ``git diff --numstat`` (insertions + deletions).
    """
    import difflib

    orig_lines = original.splitlines()
    new_lines = new.splitlines()
    matcher = difflib.SequenceMatcher(None, orig_lines, new_lines, autojunk=False)
    changed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            # A modified block counts once per line (max of removed/added), so a
            # one-line edit is 1, matching the "lines that differ" semantics.
            changed += max(i2 - i1, j2 - j1)
        elif tag == "delete":
            changed += i2 - i1
        elif tag == "insert":
            changed += j2 - j1
    return changed


def identify_improvements(
    client: Any,
    codebase_summary: str,
    test_results: RunnerOutcome,
    history: List[EvaluationRecord],
    model: str = DEFAULT_OPENAI_MODEL,
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
    result, err = llm.identify_improvements(
        client, codebase_summary, test_summary, history_summary,
        model=model, additional_context=additional_context,
    )

    if err:
        log.warning("[improve] LLM error during identification: %s", err)
        return None
    if not result or result.get("task_type") == "none":
        return None

    return ImprovementTask.from_llm_response(result)


def plan_improvement(
    client: Any,
    task: ImprovementTask,
    relevant_code: Dict[str, str],
    model: str = DEFAULT_OPENAI_MODEL,
    on_error: Optional[Callable[[str], None]] = None,
) -> tuple[Optional[str], Optional[dict]]:
    """Generate a plan for the improvement. Returns (plan, usage).

    `on_error` distinguishes "the model returned nothing" from "the call
    failed" -- see llm.chat_completion.
    """
    code_text = "\n\n".join(
        f"### {path}\n{content}" for path, content in relevant_code.items()
    )
    task_dict = {
        "task_type": task.task_type,
        "description": task.description,
        "target_files": task.target_files,
        "evidence": task.evidence,
    }
    return llm.plan_code_change(client, task_dict, code_text, model=model,
                                on_error=on_error)


def generate_changes(
    client: Any,
    task: ImprovementTask,
    plan: str,
    file_contents: Dict[str, str],
    config: SafetyConfig,
    model: str = DEFAULT_OPENAI_MODEL,
) -> tuple[Optional[List[CodeChange]], Optional[dict]]:
    """Generate code changes from a plan. Returns (changes, usage)."""
    # Agent-mode backend: let the CLI edit the working tree directly, then take
    # its diff as the change set. On failure we fall through to the LLM path so
    # the autonomous loop never wedges. agent_generate_changes resets the tree,
    # so the LLM fallback (and the downstream validate step) start clean.
    backend = getattr(config, "generator_backend", "openai")
    if backends.is_cli_backend(backend):
        repo_root = get_repo_root()
        changes, usage = backends.agent_generate_changes(
            task, plan, repo_root, config, backend,
            model=getattr(config, "generator_model", None),
        )
        if changes:
            log.info("[improve] agent backend '%s' produced %d change(s)", backend, len(changes))
            return changes, usage
        log.warning("[improve] agent backend '%s' yielded no changes; falling back to LLM", backend)

    constraints = (
        f"- Maximum {config.max_changed_files_per_pr} files\n"
        f"- Maximum {config.max_lines_changed_per_pr} lines changed\n"
        f"- Only modify files under: {', '.join(config.allowed_modification_paths)}\n"
        f"- NEVER modify: {', '.join(config.forbidden_modification_paths)}\n"
        f"- Task type: {task.task_type}"
    )

    raw_changes, usage = llm.generate_code(client, plan, file_contents, constraints, model=model)
    if not raw_changes:
        return None, usage

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

    return changes, usage


def _resolved_inside(repo_root: Path, file_path: str, config: SafetyConfig) -> Path:
    """Resolve a change path and re-judge what it actually names.

    The policy checks are lexical, which cannot see a symlink: a component
    pointing outside the tree satisfies every prefix rule while naming a file
    somewhere else. Only the filesystem can answer that, and only here, where
    repo_root is known.

    Landing inside the repository is not sufficient either. A symlink at
    src/ouroboros/link.py pointing to src/ouroboros/config.py resolves to a
    path that is inside and passes every name-based check, while the write
    goes to the agent's own SafetyConfig. So the resolved path is put back
    through the same gate as the one that was asked for.
    """
    root = repo_root.resolve()
    full_path = (repo_root / file_path).resolve()

    if full_path != root and root not in full_path.parents:
        raise PermissionError(
            f"Path escapes the repository: {file_path} -> {full_path}"
        )

    resolved_relative = full_path.relative_to(root).as_posix()
    if not _is_path_allowed(resolved_relative, config):
        raise PermissionError(
            f"Path resolves to a forbidden file: {file_path} -> {resolved_relative}"
        )
    return full_path


def apply_changes(
    changes: List[CodeChange],
    repo_root: Path,
    config: SafetyConfig | None = None,
) -> None:
    """Write changes to disk. Raises on forbidden paths.

    Takes the config the caller validated against. Defaulting to a fresh
    SafetyConfig meant a caller with custom allowed paths passed validation and
    was then refused at the write.
    """
    config = config or SafetyConfig()
    for change in changes:
        if not _is_path_allowed(change.file_path, config):
            raise PermissionError(f"Cannot modify forbidden file: {change.file_path}")

        full_path = _resolved_inside(repo_root, change.file_path, config)
        # Ground truth for the revert: taken from the filesystem here because
        # this is the last moment the pre-change tree still exists.
        change.existed_before = full_path.exists()
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(change.new_content, encoding="utf-8")


def revert_changes(changes: List[CodeChange], repo_root: Path) -> None:
    """Undo what ``apply_changes`` wrote, in reverse order of application.

    Reverse order because two entries can name the same file (a create
    followed by an edit of it). Applied forwards, the create is undone first
    -- the file is unlinked -- and then the edit resurrects it, leaving a
    stray file behind and the worktree dirty, which blocks every later cycle.
    """
    for change in reversed(changes):
        # An existing empty file carries original_content == "" just like a
        # brand new one, so emptiness cannot say whether the file is ours to
        # delete (#91). Only the filesystem can, and apply_changes recorded
        # what it saw there. None means this change never reached
        # apply_changes -- nothing was written, so there is nothing to undo,
        # and touching the file would clobber content we never wrote.
        if change.existed_before is None:
            continue
        full_path = repo_root / change.file_path
        if change.existed_before:
            full_path.write_text(change.original_content, encoding="utf-8")
        elif full_path.exists():
            # File was newly created, remove it
            full_path.unlink()


def _format_failure_details(test_result: RunnerOutcome) -> str:
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
    test_before: RunnerOutcome,
    test_after: RunnerOutcome,
    config: SafetyConfig,
    repo_root: Path,
    model: str = DEFAULT_OPENAI_MODEL,
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

    raw_changes, usage = llm.generate_code(client, retry_prompt, original_code, constraints, model=model)
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
        apply_changes(retry_changes, repo_root, config)
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
    if usage:
        result.total_usage["prompt_tokens"] = usage.get("prompt_tokens", 0)
        result.total_usage["completion_tokens"] = usage.get("completion_tokens", 0)
    return result


def validate_improvement(
    task: ImprovementTask,
    changes: List[CodeChange],
    repo_root: Path,
    *,
    client: Any = None,
    config: SafetyConfig | None = None,
    model: str = DEFAULT_OPENAI_MODEL,
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

    # Hard safety gate: if the baseline test run did not actually execute any
    # tests (e.g. a misconfigured runner returning a usage error), we cannot
    # detect regressions -- so refuse to proceed rather than merge unvalidated.
    if result.test_before.total == 0 and not result.test_before.success:
        log.error(
            "Baseline tests did not run (%s); refusing to validate/merge",
            result.test_before.summary(),
        )
        result.details = (
            f"Cannot validate: baseline test run produced no results "
            f"({result.test_before.summary()})"
        )
        result.status = "failed"
        return result

    # Validate safety constraints
    violations = _validate_changes(changes, config)
    if violations:
        log.warning("Safety violations: %s", violations)
        result.details = "; ".join(violations)
        result.status = "failed"
        return result

    # Apply changes
    try:
        apply_changes(changes, repo_root, config)
    except PermissionError as e:
        log.error("Permission denied: %s", e)
        result.details = str(e)
        result.status = "failed"
        return result

    # Run tests after changes
    result.test_after = run_tests(repo_root)
    log.info("Tests after: %s", result.test_after.summary())

    # Check for regression (failures/errors)
    has_regression = (
        result.test_after.failed > result.test_before.failed
        or result.test_after.errors > result.test_before.errors
    )

    # Check for coverage regression (Quality Gate)
    if result.test_before.coverage_percent is not None and result.test_after.coverage_percent is not None:
        cov_delta = result.test_before.coverage_percent - result.test_after.coverage_percent
        if cov_delta > 1.0:
            log.warning("Coverage regression detected: %.1f%% drop", cov_delta)
            result.details = f"Coverage regression: dropped from {result.test_before.coverage_percent}% to {result.test_after.coverage_percent}%"
            revert_changes(changes, repo_root)
            result.status = "reverted"
            return result

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


class ToolRunner:
    """Helper to execute tools called by the LLM."""
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root

    def execute(self, name: str, args: dict) -> str:
        if name == "grep_codebase":
            pattern = args.get("pattern", "")
            try:
                import subprocess
                # grep exits 1 when it simply found nothing; only >=2 is a real error.
                proc = subprocess.run(
                    ["grep", "-r", "-n", "--include=*.py", pattern, str(self.repo_root / "src")],
                    text=True, capture_output=True
                )
                if proc.returncode == 0:
                    return proc.stdout or "No matches found."
                if proc.returncode == 1:
                    return "No matches found."
                return f"Error running grep: {proc.stderr.strip()}"
            except Exception as e:
                return f"Error running grep: {e}"
        elif name == "read_file_metadata":
            path = args.get("file_path", "")
            try:
                from .codebase import read_file_raw, extract_code_metadata
                full_path = self.repo_root / path
                content = read_file_raw(full_path)
                meta = extract_code_metadata(content, path)
                return str(meta)
            except Exception as e:
                return f"Error reading metadata: {e}"
        elif name == "read_file_content":
            path = args.get("file_path", "")
            try:
                from .codebase import read_file_raw
                return read_file_raw(self.repo_root / path)
            except Exception as e:
                return f"Error reading file: {e}"
        elif name == "run_tests":
            try:
                from .test_runner import run_tests
                res = run_tests(self.repo_root)
                return res.summary()
            except Exception as e:
                return f"Error running tests: {e}"
        elif name == "read_system_logs":
            try:
                from .system import get_service_logs
                return get_service_logs(args.get("lines", 50))
            except Exception as e:
                return f"Error reading logs: {e}"
        elif name == "check_system_health":
            try:
                from .system import get_system_summary
                return get_system_summary()
            except Exception as e:
                return f"Error checking health: {e}"
        return f"Unknown tool: {name}"


# How much a task must resemble the backlog item it was offered before the item
# is considered taken. Calibrated against real history: a genuine restatement of
# the same task scores ~0.83, while two different tasks on neighbouring modules
# ("unit tests for memory" vs "unit tests for backlog") score ~0.60.
_BACKLOG_MATCH_THRESHOLD = 0.8


# How close a proposed task must be to a COMPLETED one before it is treated as
# already done. Same scale and calibration as _BACKLOG_MATCH_THRESHOLD.
_DUPLICATE_THRESHOLD = 0.8


def _already_completed(task: Any, history: List[EvaluationRecord]) -> Optional[str]:
    """The description of a completed task this one duplicates, or None.

    Deliberately narrow. Broad FTS matching is not usable as this gate: two
    unrelated tasks sharing "test", "parser" and "memory" would score highly and
    real work would be suppressed -- a silent failure, and far worse than the
    duplication it prevents. Only an exact task id or >= 0.8 content-word
    overlap against SUCCEEDED history counts.

    Failed attempts deliberately do not count. A task that failed should be
    retried; discouraging it is the job of the "previously failed" context,
    which advises the model rather than overriding it.
    """
    description = getattr(task, "description", "") or ""
    if not description.strip():
        return None
    from . import backlog as _backlog

    task_id = getattr(task, "task_id", None)
    for record in history:
        if record.outcome not in ("merged", "success"):
            continue
        if task_id and getattr(record, "task_id", None) == task_id:
            return record.description
        if _backlog.content_overlap(description, record.description or "") >= _DUPLICATE_THRESHOLD:
            return record.description
    return None


def _record_cycle_memory(ctx: Dict[str, Any]) -> None:
    """Write the cycle's outcome into memory. Runs on EVERY path.

    This block used to sit at the bottom of the cycle, after PR creation. Four
    of the eleven exits -- no plan (~975), no code (~991), reviewer rejected
    (~1025), validation failed (~1058) -- return before reaching it, so a cycle
    that failed recorded nothing. Measured on production: 544 facts in
    categories `code` and `success` only, and **zero failure facts in five
    months**. The agent has never once recorded why something did not work,
    which is the category it most needs in order to stop retrying it.

    Note what is NOT moved here. `record_improvement` and `_append_learning`
    are already called at all five exits -- duplicated, but not bypassed, so
    hoisting them would be a reordering risk for no correctness gain. Only the
    memory write was actually unreachable, so only it moves.

    Called from a `finally`, so it must not raise: a failure to record must not
    turn a completed cycle into a crashed one.
    """
    task, result = ctx.get("task"), ctx.get("result")
    if task is None or result is None:
        return  # a legitimate skip -- rate limit, open PR, stale streak, dry run
    try:
        from .memory import IndexManager
        memory = IndexManager()
        if result.status == "success":
            for change in result.changes:
                memory.index_file(change.file_path, change.new_content)
            memory.index_success(task.task_id, task.description, result.details or "")
            memory.record_outcome_feedback(task.description, success=True)
        elif result.status in ("failed", "reverted"):
            memory.index_failure(task.task_id, task.description, result.details or "")
            memory.record_outcome_feedback(task.description, success=False)
    except Exception:
        log.debug("Memory indexing failed", exc_info=True)


def _finalize_backlog(ctx: Dict[str, Any]) -> None:
    """Close out the backlog item this cycle was offered, if it took it.

    `mark_done` and `mark_failed` had zero production callers. An item was
    injected into the prompt as HIGH-PRIORITY and then never resolved, so the
    same head was re-offered every cycle indefinitely.

    The link between the offered item and the task the model came back with is
    not exact -- nothing round-trips an id through the prompt -- so it is
    established by content overlap, and a weak match resolves nothing. Marking
    the wrong item done would silently drop real work from the agenda, which is
    worse than leaving an item open one more cycle.

    `mark_failed` already counts attempts and abandons at three; it is called
    once per failed attempt, not gated on a count here.

    Runs from a `finally` and must not raise.
    """
    item = ctx.get("backlog_item")
    task, result = ctx.get("task"), ctx.get("result")
    repo_root = ctx.get("repo_root")
    if not item or task is None or result is None or repo_root is None:
        return
    try:
        from . import backlog as _backlog

        overlap = _backlog.content_overlap(
            item.get("description", ""), getattr(task, "description", "")
        )
        if overlap < _BACKLOG_MATCH_THRESHOLD:
            log.debug("Cycle did not take the offered backlog item (overlap %.2f)", overlap)
            return
        item_id = item.get("id")
        if not item_id:
            return
        if result.status == "success":
            _backlog.mark_done(repo_root, item_id)
        elif result.status in ("failed", "reverted"):
            _backlog.mark_failed(repo_root, item_id)
        # Anything else -- "skipped" (a dry run, improvement.py:1121) or the
        # "pending" default (line 69, still set when an exception escapes the
        # body after the result exists) -- is NOT an attempt and must not count
        # as one. An `else` here meant three dry-run preflights, or three cycles
        # during a backend outage like the three-week agy one in August, would
        # silently abandon a live item. That is the failure this function's own
        # threshold exists to prevent, arriving through the back door.
    except Exception:
        log.debug("Backlog finalisation failed", exc_info=True)


def run_improvement_cycle(
    client: Any,
    state: Dict[str, Any],
    config: SafetyConfig | None = None,
    model: str = DEFAULT_OPENAI_MODEL,
    dry_run: bool = False,
    on_event: EventCallback = None,
) -> Optional[ImprovementResult]:
    """Run a full improvement cycle, and record its outcome whatever happens.

    A thin wrapper so that the memory write has exactly one call site reachable
    from every exit of `_run_improvement_cycle`, including the ones that return
    early and the ones that raise. Adding a twelfth exit to the body below
    cannot reintroduce the bypass; that is the whole point of the split.
    """
    ctx: Dict[str, Any] = {}
    try:
        return _run_improvement_cycle(
            ctx, client, state, config=config, model=model,
            dry_run=dry_run, on_event=on_event,
        )
    finally:
        _record_cycle_memory(ctx)
        _finalize_backlog(ctx)


def _run_improvement_cycle(
    ctx: Dict[str, Any],
    client: Any,
    state: Dict[str, Any],
    config: SafetyConfig | None = None,
    model: str = DEFAULT_OPENAI_MODEL,
    dry_run: bool = False,
    on_event: EventCallback = None,
) -> Optional[ImprovementResult]:
    """Run a full improvement cycle with tool-calling ReAct loop.

    Populates `ctx` with the task and result as soon as each exists, so the
    caller's `finally` can record the outcome no matter which exit is taken.
    """
    config = config or SafetyConfig()
    repo_root = get_repo_root()
    ctx["repo_root"] = repo_root
    tool_runner = ToolRunner(repo_root)

    # Per-role backend clients. identify/plan run as text/JSON completions; a
    # CLI client returns tool_calls=None so identify cleanly skips the OpenAI
    # ReAct tool-loop and uses the JSON path. generate/review route to their
    # own backends downstream (generate_changes / make_backend_client).
    identify_client = backends.make_backend_client(
        getattr(config, "identify_backend", "openai"), openai_client=client
    )
    plan_client = backends.make_backend_client(
        getattr(config, "plan_backend", "openai"), openai_client=client
    )

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
    open_prs = git_ops.has_open_improvement_prs(repo_root)
    if open_prs is not False:
        # None means the lookup failed; defer rather than risk a second PR for
        # work already in flight.
        reason = (
            "open improvement PRs exist" if open_prs
            else "could not determine whether an improvement PR is open"
        )
        log.info("Skipping improvement: %s", reason)
        _fire("cycle_end", f"Skipped: {reason}")
        return None

    # Dedup: skip if recent attempts at the same task_type made no test progress
    history = load_history(repo_root)
    _MAX_STALE_ATTEMPTS = 2
    recent = [r for r in history if r.timestamp > time.time() - 7 * 86400]
    if recent:
        last_type = recent[-1].task_type
        streak = [r for r in reversed(recent) if r.task_type == last_type]
        if len(streak) >= _MAX_STALE_ATTEMPTS:
            all_no_progress = all(
                r.test_delta.get("before") == r.test_delta.get("after")
                for r in streak[:_MAX_STALE_ATTEMPTS]
            )
            if all_no_progress:
                log.info("Skipping improvement: %d recent '%s' attempts with no test progress", len(streak), last_type)
                _fire("cycle_end", f"Skipped: {len(streak)} stale '{last_type}' attempts")
                return None

    # Step 1: Understand the codebase
    log.info("[improve] Analyzing codebase...")
    codebase_summary = get_codebase_summary(repo_root)
    test_results = run_tests(repo_root)

    # Step 2: Identify an improvement (with tool support)
    log.info("[improve] Identifying improvements...")
    history_summary = summarize_history(history)
    
    # Inject system awareness
    from .system import get_system_summary
    from .memory import IndexManager
    system_ctx = f"### System Environment\n{get_system_summary()}\n"

    # Inject semantic memory (local HRR -- no API calls)
    memory = IndexManager()
    memory_ctx = memory.retrieve_relevant_context(codebase_summary[:1000])

    additional_context = _assemble_feed_context(client, state)
    final_ctx = f"{system_ctx}\n{memory_ctx}\n{additional_context}" if additional_context else f"{system_ctx}\n{memory_ctx}"

    # Failure-driven task prioritization
    if test_results.failed > 0 or test_results.errors > 0:
        n_failing = test_results.failed + test_results.errors
        failure_lines = ""
        if test_results.failure_details:
            failure_lines = "\n".join(
                f"  - {f.file}::{f.test_name}: {f.message}"
                for f in test_results.failure_details
            )
        priority_ctx = (
            f"PRIORITY: There are currently {n_failing} failing tests. "
            f"Prioritize fixing them before any other task type.\n"
            f"Failing tests:\n{failure_lines}"
        )
        final_ctx = f"{priority_ctx}\n\n{final_ctx}"

    # Inject high-priority backlog item if present
    try:
        from . import backlog as _backlog
        pending = _backlog.get_pending(repo_root)
        if pending and pending[0].get("priority", 0) >= 8:
            top = pending[0]
            # Remembered so the epilogue can close it out. Without this the item
            # is offered as HIGH-PRIORITY every cycle forever -- nothing ever
            # called mark_done, which is why "code-aware indexing in MemoryStore"
            # was implemented on 2026-06-27 and again on 2026-08-22, the second
            # time re-implementing a method that already existed (tests 966->966).
            ctx["backlog_item"] = top
            backlog_ctx = (
                f"HIGH-PRIORITY BACKLOG ITEM: [{top.get('task_type', '?')}] "
                f"{top.get('description', '')} (priority={top.get('priority', '?')})"
            )
            final_ctx = f"{backlog_ctx}\n\n{final_ctx}"
    except Exception:
        log.debug("Backlog context injection failed")

    # Inject recent learning history
    recent_learnings = _read_recent_learnings(repo_root)
    if recent_learnings:
        learnings_ctx = f"Recent improvement history (last 20):\n{recent_learnings}"
        final_ctx = f"{final_ctx}\n\n{learnings_ctx}"

    # Written, unit-tested, and never called until now. Without them the model
    # re-proposes work it has already failed at, with no idea it ever tried.
    failed_ctx = _build_failed_attempts_context(history)
    if failed_ctx:
        final_ctx = f"{final_ctx}\n\n{failed_ctx}"
    rates_ctx = _build_success_rate_context(history)
    if rates_ctx:
        final_ctx = f"{final_ctx}\n\n{rates_ctx}"

    task_data, id_err = llm.identify_improvements(
        identify_client, codebase_summary, test_results.summary(), history_summary,
        model=model, additional_context=final_ctx
    )

    if id_err:
        log.warning("[improve] LLM error during identification: %s", id_err)
        _fire("failed", f"LLM error: {id_err}")
        return None
    if not task_data:
        log.info("[improve] No improvements identified")
        _fire("cycle_end", "No improvements identified")
        return None

    # Handle Tool Calls -- multi-step ReAct loop (up to 5 rounds)
    if "_tool_calls" in task_data:
        tool_calls = task_data["_tool_calls"]
        messages = [
            {"role": "system", "content": "You are a code quality analyst."},
            {"role": "assistant", "tool_calls": tool_calls}
        ]
        for tool_call in tool_calls:
            tool_result = tool_runner.execute(tool_call.function.name, json.loads(tool_call.function.arguments))
            messages.append({
                "tool_call_id": tool_call.id,
                "role": "tool",
                "name": tool_call.function.name,
                "content": tool_result,
            })

        _MAX_REACT_ROUNDS = 5
        for react_round in range(1, _MAX_REACT_ROUNDS + 1):
            log.info("[improve] ReAct round %d/%d", react_round, _MAX_REACT_ROUNDS)
            resp = llm.create_completion(
                identify_client,
                model=model,
                messages=messages,
                response_format={"type": "json_object"}
            )
            msg = resp.choices[0].message
            # If no tool calls, treat as final answer
            if not msg.tool_calls:
                task_data = json.loads(msg.content)
                if resp.usage:
                    task_data["_usage"] = {
                        "prompt_tokens": resp.usage.prompt_tokens,
                        "completion_tokens": resp.usage.completion_tokens,
                        "total_tokens": resp.usage.total_tokens,
                    }
                break
            # More tool calls -- execute and feed results back
            messages.append({"role": "assistant", "tool_calls": msg.tool_calls})
            for tool_call in msg.tool_calls:
                tool_result = tool_runner.execute(tool_call.function.name, json.loads(tool_call.function.arguments))
                messages.append({
                    "tool_call_id": tool_call.id,
                    "role": "tool",
                    "name": tool_call.function.name,
                    "content": tool_result,
                })
            if react_round == _MAX_REACT_ROUNDS:
                log.info("[improve] ReAct loop hit max rounds (%d), forcing final answer", _MAX_REACT_ROUNDS)
                # Force a final answer without tools
                resp = llm.create_completion(
                identify_client,
                    model=model,
                    messages=messages,
                    response_format={"type": "json_object"}
                )
                task_data = json.loads(resp.choices[0].message.content)
                if resp.usage:
                    task_data["_usage"] = {
                        "prompt_tokens": resp.usage.prompt_tokens,
                        "completion_tokens": resp.usage.completion_tokens,
                        "total_tokens": resp.usage.total_tokens,
                    }

    task = ImprovementTask.from_llm_response(task_data)
    ctx["task"] = task

    duplicate_of = _already_completed(task, history)
    if duplicate_of is not None:
        log.info("[improve] Skipping: already completed -- %s", duplicate_of[:100])
        _fire("cycle_end", f"Skipped: already completed ({duplicate_of[:80]})")
        _append_learning(
            repo_root,
            f"{_today()} | {task.task_type} | {task.description[:60]} | duplicate | "
            f"already completed: {duplicate_of[:60]}",
        )
        return None
    log.info("[improve] Identified: [%s] %s", task.task_type, task.description)
    _fire("task_identified", f"Task: [{task.task_type}] {task.description}", {
        "task_type": task.task_type,
        "description": task.description,
        "target_files": task.target_files,
    })

    improvement_result = ImprovementResult(task=task)
    ctx["result"] = improvement_result
    if task._usage:
        improvement_result.total_usage["prompt_tokens"] += task._usage.get("prompt_tokens", 0)
        improvement_result.total_usage["completion_tokens"] += task._usage.get("completion_tokens", 0)

    if dry_run:
        improvement_result.status = "skipped"
        log.info("[improve] Dry run -- would proceed with: %s", task.description)
        return improvement_result

    # Step 3: Read target files
    relevant_code = {}
    for file_path in task.target_files:
        full_path = repo_root / file_path
        relevant_code[file_path] = read_file_raw(full_path) if full_path.exists() else ""

    # Step 4: Plan the improvement
    log.info("[improve] Planning changes...")
    _fire("planning", f"Planning: {task.description[:100]}")
    plan_errors: List[str] = []
    plan, plan_usage = plan_improvement(
        plan_client, task, relevant_code, model=model, on_error=plan_errors.append
    )
    if plan_usage:
        improvement_result.total_usage["prompt_tokens"] += plan_usage.get("prompt_tokens", 0)
        improvement_result.total_usage["completion_tokens"] += plan_usage.get("completion_tokens", 0)

    if not plan:
        # Say WHICH failure it was. "no plan generated" masked three weeks of
        # failed agy calls in August 2026 because the two were indistinguishable.
        cause = f" ({plan_errors[0]})" if plan_errors else ""
        log.warning("[improve] Failed to generate plan%s", cause)
        improvement_result.status = "failed"
        improvement_result.details = (
            f"Planning call failed: {plan_errors[0]}" if plan_errors
            else "Failed to generate a concrete implementation plan (model returned nothing)"
        )
        _fire("failed", f"Failed to plan: {task.description[:100]}")
        record_improvement(improvement_result, repo_root, model=model)
        _append_learning(repo_root, f"{_today()} | {task.task_type} | {task.description[:60]} | failed | " + (f"planning call failed: {plan_errors[0][:60]}" if plan_errors else "no plan generated"))
        return improvement_result

    # Step 5: Generate code changes
    log.info("[improve] Generating code changes...")
    _fire("generating", f"Generating code for: {task.description[:100]}")
    changes, gen_usage = generate_changes(client, task, plan, relevant_code, config, model=model)
    if gen_usage:
        improvement_result.total_usage["prompt_tokens"] += gen_usage.get("prompt_tokens", 0)
        improvement_result.total_usage["completion_tokens"] += gen_usage.get("completion_tokens", 0)

    if not changes:
        log.warning("[improve] Failed to generate code changes")
        improvement_result.status = "failed"
        _fire("failed", f"Failed to generate code: {task.description[:100]}")
        record_improvement(improvement_result, repo_root, model=model)
        _append_learning(repo_root, f"{_today()} | {task.task_type} | {task.description[:60]} | failed | no code generated")
        return improvement_result

    improvement_result.changes = changes

    # Step 5.5: Peer Review (Consensus)
    if config.reviewer_model:
        log.info("[improve] Requesting peer review from %s...", config.reviewer_model)
        _fire("reviewing", f"Reviewing changes with {config.reviewer_model}")
        
        review_client = backends.make_backend_client(
            getattr(config, "reviewer_backend", "openai"),
            openai_client=client,
            model=config.reviewer_model,
            base_url=getattr(config, "reviewer_base_url", None),
            api_key=getattr(config, "reviewer_api_key", None),
        )
        approved, feedback, rev_usage = llm.review_code_changes(
            review_client,
            {"description": task.description},
            [{"file_path": c.file_path, "new_content": c.new_content, "description": c.description} for c in changes],
            model=config.reviewer_model
        )

        if rev_usage:
            improvement_result.total_usage["prompt_tokens"] += rev_usage.get("prompt_tokens", 0)
            improvement_result.total_usage["completion_tokens"] += rev_usage.get("completion_tokens", 0)

        if not approved:
            log.warning("[improve] Reviewer REJECTED changes: %s", feedback)
            _fire("review_failed", f"Reviewer rejected changes: {feedback[:100]}")
            improvement_result.status = "failed"
            improvement_result.details = f"Reviewer rejection: {feedback}"
            record_improvement(improvement_result, repo_root, model=model)
            _append_learning(repo_root, f"{_today()} | {task.task_type} | {task.description[:60]} | failed | reviewer rejected")
            return improvement_result
        else:
            log.info("[improve] Reviewer approved changes.")
            _fire("review_passed", "Peer review approved changes.")

    # Step 6: Validate (apply, test, revert if needed, retry on regression)
    log.info("[improve] Validating changes...")
    _fire("validating", f"Validating: {task.description[:100]}")
    validation_result = validate_improvement(
        task, changes, repo_root,
        client=client, config=config, model=model,
        on_event=on_event,
    )
    # Merge validation result
    improvement_result.test_before = validation_result.test_before
    improvement_result.test_after = validation_result.test_after
    improvement_result.status = validation_result.status
    improvement_result.details = validation_result.details
    improvement_result.changes = validation_result.changes

    if improvement_result.status == "reverted":
        _fire("reverted", f"Reverted: {task.description[:80]}\n{improvement_result.details}")

    if improvement_result.status != "success":
        log.warning("[improve] Improvement failed: %s", improvement_result.status)
        record_improvement(improvement_result, repo_root, model=model)
        before_str = improvement_result.test_before.summary() if improvement_result.test_before else "?"
        after_str = improvement_result.test_after.summary() if improvement_result.test_after else "?"
        reason = improvement_result.details[:80] if improvement_result.details else improvement_result.status
        _append_learning(
            repo_root,
            f"{_today()} | {task.task_type} | {task.description[:60]} | {improvement_result.status} | {reason}",
        )
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
        })

        if config.enable_auto_merge and pr_url:
            log.info("[improve] Attempting auto-merge...")
            if git_ops.auto_merge_pr(repo_root, pr_url):
                _fire("auto_merged", f"Auto-merged: {pr_url}")

    except Exception:
        log.exception("[improve] Failed to create PR")
        improvement_result.status = "failed"
        improvement_result.details = "PR creation failed"
        _fire("failed", "PR creation failed")
    finally:
        git_ops.checkout_branch(repo_root, original_branch)

    record_improvement(improvement_result, repo_root, model=model)

    # Append to learnings pattern library
    try:
        before_passed = improvement_result.test_before.passed if improvement_result.test_before else "?"
        after_passed = improvement_result.test_after.passed if improvement_result.test_after else "?"
        if improvement_result.status == "success":
            _append_learning(
                repo_root,
                f"{_today()} | {task.task_type} | {task.description[:60]} | success | tests: {before_passed} -> {after_passed}",
            )
        else:
            reason = improvement_result.details[:80] if improvement_result.details else improvement_result.status
            _append_learning(
                repo_root,
                f"{_today()} | {task.task_type} | {task.description[:60]} | {improvement_result.status} | {reason}",
            )
    except Exception:
        log.debug("Failed to append learning entry")

    _fire("cycle_end", f"Cycle complete: {improvement_result.status}")
    return improvement_result


def _today() -> str:
    """Return today's date as YYYY-MM-DD."""
    return datetime.date.today().isoformat()


def _append_learning(repo_root: Path, entry: str) -> None:
    """Append a one-line learning entry to config/learnings.md, capped at 100 lines."""
    learnings_path = repo_root / "config" / "learnings.md"
    learnings_path.parent.mkdir(parents=True, exist_ok=True)

    existing_lines: List[str] = []
    if learnings_path.exists():
        existing_lines = learnings_path.read_text(encoding="utf-8").splitlines()

    existing_lines.append(entry)

    # Cap at 100 lines (trim oldest from top)
    if len(existing_lines) > 100:
        existing_lines = existing_lines[-100:]

    learnings_path.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")


def _read_recent_learnings(repo_root: Path, n: int = 20) -> str:
    """Read the last n lines from config/learnings.md. Returns empty string if file absent."""
    learnings_path = repo_root / "config" / "learnings.md"
    if not learnings_path.exists():
        return ""
    lines = learnings_path.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[-n:]) if lines else ""


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
