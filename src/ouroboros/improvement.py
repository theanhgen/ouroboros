"""Core self-improvement engine -- identify, plan, generate, validate, PR."""

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

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


@dataclass
class ImprovementTask:
    task_id: str
    task_type: str  # fix_test | add_test | fix_bug
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
    result = llm.analyze_codebase(client, codebase_summary, test_summary, history_summary, model=model)

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


def validate_improvement(
    task: ImprovementTask,
    changes: List[CodeChange],
    repo_root: Path,
) -> ImprovementResult:
    """Apply changes, run tests, revert if tests regress.

    Returns an ImprovementResult with test_before, test_after, and status.
    """
    result = ImprovementResult(task=task, changes=changes)

    # Run tests before changes
    result.test_before = run_tests(repo_root)
    log.info("Tests before: %s", result.test_before.summary())

    # Validate safety constraints
    config = SafetyConfig()
    violations = _validate_changes(changes, config)
    if violations:
        log.warning("Safety violations: %s", violations)
        result.status = "failed"
        return result

    # Apply changes
    try:
        apply_changes(changes, repo_root)
    except PermissionError as e:
        log.error("Permission denied: %s", e)
        result.status = "failed"
        return result

    # Run tests after changes
    result.test_after = run_tests(repo_root)
    log.info("Tests after: %s", result.test_after.summary())

    # Check for regression
    if result.test_after.failed > result.test_before.failed:
        log.warning(
            "Test regression detected (%d -> %d failures), reverting",
            result.test_before.failed,
            result.test_after.failed,
        )
        revert_changes(changes, repo_root)
        result.status = "reverted"
        return result

    if result.test_after.errors > result.test_before.errors:
        log.warning(
            "New test errors detected (%d -> %d), reverting",
            result.test_before.errors,
            result.test_after.errors,
        )
        revert_changes(changes, repo_root)
        result.status = "reverted"
        return result

    result.status = "success"
    return result


def run_improvement_cycle(
    client: Any,
    state: Dict[str, Any],
    config: SafetyConfig | None = None,
    model: str = "gpt-4o",
    dry_run: bool = False,
) -> Optional[ImprovementResult]:
    """Run a full improvement cycle: identify -> plan -> generate -> validate -> PR.

    Returns ImprovementResult or None if no improvement was identified/attempted.
    """
    config = config or SafetyConfig()
    repo_root = get_repo_root()

    # Rate limiting
    today_count = improvements_today(repo_root)
    if today_count >= config.max_improvements_per_day:
        log.info("Rate limit reached: %d improvements today (max %d)", today_count, config.max_improvements_per_day)
        return None

    # Check for open PRs
    if git_ops.has_open_improvement_prs(repo_root):
        log.info("Skipping improvement: open improvement PRs exist")
        return None

    # Step 1: Understand the codebase
    log.info("[improve] Analyzing codebase...")
    codebase_summary = get_codebase_summary(repo_root)
    test_results = run_tests(repo_root)
    history = load_history(repo_root)

    # Step 2: Identify an improvement
    log.info("[improve] Identifying improvements...")
    task = identify_improvements(client, codebase_summary, test_results, history, model=model)
    if not task:
        log.info("[improve] No improvements identified")
        return None

    log.info("[improve] Identified: [%s] %s", task.task_type, task.description)

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
    plan = plan_improvement(client, task, relevant_code, model=model)
    if not plan:
        log.warning("[improve] Failed to generate plan")
        return None

    # Step 5: Generate code changes
    log.info("[improve] Generating code changes...")
    changes = generate_changes(client, task, plan, relevant_code, config, model=model)
    if not changes:
        log.warning("[improve] Failed to generate code changes")
        return None

    # Step 6: Validate (apply, test, revert if needed)
    log.info("[improve] Validating changes...")
    improvement_result = validate_improvement(task, changes, repo_root)

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
        changed_files = [c.file_path for c in changes]
        commit_msg = f"ouroboros: {task.task_type} - {task.description}"
        git_ops.commit_changes(repo_root, commit_msg, changed_files)
        git_ops.push_branch(repo_root, branch_name)

        pr_body = _build_pr_body(task, changes, improvement_result)
        pr_url = git_ops.create_pr(
            repo_root,
            title=f"[ouroboros] {task.task_type}: {task.description[:60]}",
            body=pr_body,
            base="main",
        )
        improvement_result.pr_url = pr_url
        log.info("[improve] PR created: %s", pr_url)

    except Exception:
        log.exception("[improve] Failed to create PR")
        improvement_result.status = "failed"
    finally:
        # Return to original branch
        git_ops.checkout_branch(repo_root, original_branch)

    record_improvement(improvement_result, repo_root)
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
        "Human review and approval required before merge.",
    ])

    return "\n".join(lines)
