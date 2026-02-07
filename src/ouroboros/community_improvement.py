"""Community-assisted self-improvement -- state machine orchestrator.

Flow:
    identified -> posted -> waiting -> analyzing -> implementing -> completed
                                          |              |
                                       fallback       failed/reverted
"""

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from . import git_ops, llm, moltbook
from .codebase import get_codebase_summary, get_repo_root, read_file_raw
from .config import SafetyConfig
from .evaluation import load_history, summarize_history
from .improvement import (
    CodeChange,
    ImprovementResult,
    ImprovementTask,
    _is_path_allowed,
    apply_changes,
    revert_changes,
    validate_improvement,
)
from .test_runner import run_tests

log = logging.getLogger(__name__)

MAX_CODE_CONTEXT_CHARS = 3000
MAX_COMMUNITY_HISTORY = 20

STATUSES = ("identified", "posted", "waiting", "analyzing", "implementing", "completed", "fallback", "failed")


def step_community_improvement(
    client: Any,
    state: Dict[str, Any],
    creds: moltbook.Credentials,
    cfg: moltbook.RunnerConfig,
    safety_config: SafetyConfig,
) -> Optional[str]:
    """Advance the community improvement state machine by one step.

    Called every loop cycle. Reads current status from state, dispatches
    to the appropriate step function.

    Returns a short status string for logging, or None if nothing happened.
    """
    ci = state.get("community_improvement")

    if ci is None:
        # Check interval
        last_start = state.get("last_community_improvement_start")
        now = int(time.time())
        interval = getattr(cfg, "community_improvement_interval_hours", 72)
        if last_start is not None and (now - int(last_start)) < interval * 3600:
            return None

        # Check no open PRs
        repo_root = get_repo_root()
        if git_ops.has_open_improvement_prs(repo_root):
            log.debug("[community] Skipping: open improvement PRs exist")
            return None

        return _step_identify(client, state, cfg, safety_config)

    status = ci.get("status")
    if status == "identified":
        return _step_post(client, state, creds, cfg)
    elif status == "posted":
        return _step_wait(state, creds, cfg)
    elif status == "waiting":
        return _step_wait(state, creds, cfg)
    elif status == "analyzing":
        return _step_analyze(client, state, cfg)
    elif status == "implementing":
        return _step_implement(client, state, creds, cfg, safety_config)
    elif status in ("completed", "failed"):
        # Should have been cleared already, but clear now
        clear_community_improvement(state)
        return f"cleared ({status})"
    elif status == "fallback":
        return _step_implement(client, state, creds, cfg, safety_config)
    else:
        log.warning("[community] Unknown status: %s, clearing", status)
        clear_community_improvement(state)
        return None


def _step_identify(
    client: Any,
    state: Dict[str, Any],
    cfg: moltbook.RunnerConfig,
    safety_config: SafetyConfig,
) -> Optional[str]:
    """Identify a real problem that would benefit from community input."""
    repo_root = get_repo_root()

    log.info("[community] Analyzing codebase for community-suitable problems...")
    codebase_summary = get_codebase_summary(repo_root)
    test_results = run_tests(repo_root)
    history = load_history(repo_root)
    history_summary = summarize_history(history)

    # Build test failure text
    test_text = test_results.summary()
    if test_results.failure_details:
        test_text += "\n\nFailure details:\n"
        for fail in test_results.failure_details:
            test_text += f"- {fail.file}::{fail.test_name}: {fail.message}\n"
            if fail.traceback:
                test_text += f"  {fail.traceback[:200]}\n"

    # Ask LLM to identify a problem suitable for community input
    task_data = llm.analyze_codebase(
        client, codebase_summary, test_text, history_summary,
        model=getattr(cfg, "improvement_model", "gpt-4o"),
    )

    if not task_data or task_data.get("task_type") == "none":
        log.info("[community] No problems identified for community input")
        state["last_community_improvement_start"] = int(time.time())
        return "no_problems"

    task = ImprovementTask.from_llm_response(task_data)

    # Read code context for target files (truncated)
    code_context = {}
    for file_path in task.target_files:
        full_path = repo_root / file_path
        if full_path.exists():
            content = read_file_raw(full_path)
            if len(content) > MAX_CODE_CONTEXT_CHARS:
                content = content[:MAX_CODE_CONTEXT_CHARS] + "\n# ... (truncated)"
            code_context[file_path] = content

    state["community_improvement"] = {
        "status": "identified",
        "task_id": task.task_id,
        "task_type": task.task_type,
        "description": task.description,
        "target_files": task.target_files,
        "evidence": task.evidence,
        "code_context": code_context,
        "test_output": test_text,
        "post_id": None,
        "posted_at": None,
        "wait_until": None,
        "comments_snapshot": [],
        "selected_comment": None,
        "fallback_used": False,
        "pr_url": None,
    }
    state["last_community_improvement_start"] = int(time.time())

    log.info("[community] Identified: [%s] %s", task.task_type, task.description)
    return "identified"


def _step_post(
    client: Any,
    state: Dict[str, Any],
    creds: moltbook.Credentials,
    cfg: moltbook.RunnerConfig,
) -> Optional[str]:
    """Generate and post a StackOverflow-style question to Moltbook."""
    ci = state["community_improvement"]

    # Check community post interval
    now = int(time.time())
    last_community_post = state.get("last_community_post")
    interval_hours = getattr(cfg, "community_post_interval_hours", 1.0)
    if last_community_post is not None:
        elapsed_hours = (now - int(last_community_post)) / 3600
        if elapsed_hours < interval_hours:
            remaining = interval_hours - elapsed_hours
            log.info(
                "[community] Too soon to post (%.1fh since last, need %.1fh). Waiting %.1fh more.",
                elapsed_hours,
                interval_hours,
                remaining,
            )
            # Stay in identified state, will retry next cycle
            return "waiting_for_interval"

    task_data = {
        "task_type": ci["task_type"],
        "description": ci["description"],
        "target_files": ci["target_files"],
        "evidence": ci["evidence"],
    }

    post_data = llm.generate_question_post(
        client,
        task_data,
        ci.get("code_context", {}),
        ci.get("test_output", ""),
        model=getattr(cfg, "improvement_model", "gpt-4o"),
    )

    if not post_data or "title" not in post_data or "content" not in post_data:
        log.warning("[community] Failed to generate question post")
        ci["status"] = "failed"
        return "post_generation_failed"

    if cfg.dry_run:
        log.info(
            "[community] [dry-run] Would post:\nTitle: %s\nContent: %s",
            post_data["title"],
            post_data["content"][:200],
        )
        ci["status"] = "completed"
        state["last_community_post"] = now
        return "dry_run_posted"

    try:
        result = moltbook.create_post(
            creds.api_key,
            cfg.default_submolt,
            post_data["title"],
            content=post_data["content"],
        )
        now = int(time.time())
        wait_hours = getattr(cfg, "community_wait_hours", 48)

        ci["post_id"] = result.get("id")
        ci["posted_at"] = now
        ci["wait_until"] = now + wait_hours * 3600
        ci["status"] = "waiting"
        state["last_community_post"] = now

        log.info(
            "[community] Posted question: %s (id: %s, waiting %dh)",
            post_data["title"],
            ci["post_id"],
            wait_hours,
        )
        return "posted"

    except Exception:
        log.exception("[community] Failed to create post")
        ci["status"] = "failed"
        return "post_failed"


def _step_wait(
    state: Dict[str, Any],
    creds: moltbook.Credentials,
    cfg: moltbook.RunnerConfig,
) -> Optional[str]:
    """Check if wait period is over or enough comments have arrived."""
    ci = state["community_improvement"]
    now = int(time.time())
    post_id = ci.get("post_id")
    wait_until = ci.get("wait_until", now)
    min_comments = getattr(cfg, "community_min_comments_for_early", 3)

    if not post_id:
        ci["status"] = "failed"
        return "no_post_id"

    # Fetch current comments
    try:
        comment_data = moltbook.get_post_comments(creds.api_key, post_id)
        comments = comment_data.get("comments", [])
        ci["comments_snapshot"] = comments
    except Exception:
        log.exception("[community] Failed to fetch comments")
        comments = ci.get("comments_snapshot", [])

    # Check early trigger: enough comments
    if len(comments) >= min_comments:
        log.info("[community] Early analysis trigger: %d comments (>= %d)", len(comments), min_comments)
        ci["status"] = "analyzing"
        return "early_analysis"

    # Check deadline
    if now >= wait_until:
        log.info("[community] Wait period expired, advancing to analysis (%d comments)", len(comments))
        ci["status"] = "analyzing"
        return "deadline_analysis"

    remaining_hours = (wait_until - now) / 3600
    log.debug(
        "[community] Still waiting: %d comments, %.1fh remaining",
        len(comments),
        remaining_hours,
    )
    return "waiting"


def _step_analyze(
    client: Any,
    state: Dict[str, Any],
    cfg: moltbook.RunnerConfig,
) -> Optional[str]:
    """Analyze comments for code suggestions, rank them, select best or fallback."""
    ci = state["community_improvement"]
    comments = ci.get("comments_snapshot", [])

    if not comments:
        log.info("[community] No comments received, falling back to LLM-only")
        ci["status"] = "fallback"
        ci["fallback_used"] = True
        return "fallback_no_comments"

    analysis = llm.analyze_code_suggestions(
        client,
        ci["description"],
        ci.get("code_context", {}),
        comments,
        model=getattr(cfg, "improvement_model", "gpt-4o"),
    )

    if not analysis or not analysis.get("has_actionable"):
        log.info("[community] No actionable suggestions in comments, falling back to LLM-only")
        ci["status"] = "fallback"
        ci["fallback_used"] = True
        return "fallback_no_actionable"

    # Select best suggestion by confidence
    suggestions = analysis.get("suggestions", [])
    suggestions.sort(key=lambda s: s.get("confidence", 0), reverse=True)
    best = suggestions[0]

    ci["selected_comment"] = {
        "author": best.get("author", "unknown"),
        "content": best.get("approach", ""),
        "comment_id": best.get("comment_id", ""),
        "code_snippets": best.get("code_snippets", []),
        "confidence": best.get("confidence", 0),
    }
    ci["status"] = "implementing"

    log.info(
        "[community] Selected suggestion by %s (confidence: %.2f): %s",
        best.get("author", "unknown"),
        best.get("confidence", 0),
        best.get("approach", "")[:100],
    )
    return "suggestion_selected"


def _step_implement(
    client: Any,
    state: Dict[str, Any],
    creds: moltbook.Credentials,
    cfg: moltbook.RunnerConfig,
    safety_config: SafetyConfig,
) -> Optional[str]:
    """Generate code from the selected suggestion (or LLM-only fallback) and create PR."""
    ci = state["community_improvement"]
    repo_root = get_repo_root()
    is_fallback = ci.get("fallback_used", False)

    # Build task object
    task = ImprovementTask(
        task_id=ci["task_id"],
        task_type=ci["task_type"],
        description=ci["description"],
        target_files=ci["target_files"],
        evidence=ci["evidence"],
    )

    # Read current file contents
    file_contents = {}
    for file_path in task.target_files:
        full_path = repo_root / file_path
        if full_path.exists():
            file_contents[file_path] = read_file_raw(full_path)
        else:
            file_contents[file_path] = ""

    # Build constraints
    constraints = (
        f"- Maximum {safety_config.max_changed_files_per_pr} files\n"
        f"- Maximum {safety_config.max_lines_changed_per_pr} lines changed\n"
        f"- Only modify files under: {', '.join(safety_config.allowed_modification_paths)}\n"
        f"- NEVER modify: {', '.join(safety_config.forbidden_modification_paths)}\n"
        f"- Task type: {task.task_type}"
    )

    # Generate plan
    plan = llm.plan_code_change(
        client,
        {
            "task_type": task.task_type,
            "description": task.description,
            "target_files": task.target_files,
            "evidence": task.evidence,
        },
        "\n\n".join(f"### {p}\n{c}" for p, c in file_contents.items()),
        model=getattr(cfg, "improvement_model", "gpt-4o"),
    )

    if not plan:
        log.warning("[community] Failed to generate plan")
        ci["status"] = "failed"
        return "plan_failed"

    # Generate code -- from suggestion or LLM-only
    if is_fallback:
        raw_changes = llm.generate_code(
            client, plan, file_contents, constraints,
            model=getattr(cfg, "improvement_model", "gpt-4o"),
        )
    else:
        suggestion = ci.get("selected_comment", {})
        raw_changes = llm.generate_code_from_suggestion(
            client, suggestion, file_contents, plan, constraints,
            model=getattr(cfg, "improvement_model", "gpt-4o"),
        )

    if not raw_changes:
        log.warning("[community] Failed to generate code")
        ci["status"] = "failed"
        return "code_generation_failed"

    # Build CodeChange objects
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

    if cfg.dry_run:
        log.info("[community] [dry-run] Would apply %d changes", len(changes))
        ci["status"] = "completed"
        return "dry_run_implemented"

    # Validate (apply, test, revert if regression)
    log.info("[community] Validating changes...")
    improvement_result = validate_improvement(task, changes, repo_root)

    if improvement_result.status != "success":
        log.warning("[community] Validation failed: %s", improvement_result.status)
        ci["status"] = "failed"
        return f"validation_{improvement_result.status}"

    # Create PR with commenter credit
    log.info("[community] Creating PR...")
    branch_name = git_ops.make_branch_name(f"community-{task.task_type}")
    original_branch = git_ops.current_branch(repo_root)

    try:
        git_ops.create_branch(repo_root, branch_name)
        changed_files = [c.file_path for c in changes]
        commit_msg = f"ouroboros: community {task.task_type} - {task.description}"
        git_ops.commit_changes(repo_root, commit_msg, changed_files)
        git_ops.push_branch(repo_root, branch_name)

        pr_body = _build_community_pr_body(task, changes, improvement_result, ci)
        pr_url = git_ops.create_pr(
            repo_root,
            title=f"[ouroboros] community {task.task_type}: {task.description[:50]}",
            body=pr_body,
            base="main",
        )
        ci["pr_url"] = pr_url
        ci["status"] = "completed"
        log.info("[community] PR created: %s", pr_url)
        return "pr_created"

    except Exception:
        log.exception("[community] Failed to create PR")
        ci["status"] = "failed"
        return "pr_failed"
    finally:
        git_ops.checkout_branch(repo_root, original_branch)


def _build_community_pr_body(
    task: ImprovementTask,
    changes: List[CodeChange],
    result: ImprovementResult,
    ci: Dict[str, Any],
) -> str:
    """Build PR description with commenter attribution."""
    is_fallback = ci.get("fallback_used", False)
    selected = ci.get("selected_comment", {})
    post_id = ci.get("post_id")

    lines = [
        "## Community-Assisted Self-Improvement",
        "",
        f"**Type**: {task.task_type}",
        f"**Task ID**: {task.task_id}",
    ]

    if post_id:
        post_url = moltbook._post_url(post_id)
        if post_url:
            lines.append(f"**Moltbook Post**: {post_url}")

    lines.extend([
        "",
        "### Description",
        task.description,
        "",
        "### Evidence",
        task.evidence,
        "",
    ])

    if not is_fallback and selected:
        author = selected.get("author", "unknown")
        lines.extend([
            "### Community Credit",
            f"Solution inspired by **{author}**'s suggestion:",
            f"> {selected.get('content', '')}",
            "",
        ])
    elif is_fallback:
        lines.extend([
            "### Note",
            "No actionable community suggestions were received. "
            "This improvement was generated by LLM-only fallback.",
            "",
        ])

    lines.append("### Changes")
    for change in changes:
        lines.append(f"- `{change.file_path}`: {change.description}")

    lines.extend([
        "",
        "### Test Results",
        f"- **Before**: {result.test_before.summary() if result.test_before else 'N/A'}",
        f"- **After**: {result.test_after.summary() if result.test_after else 'N/A'}",
        "",
        "---",
        "Generated by Ouroboros community-assisted self-improvement engine.",
        "Human review and approval required before merge.",
    ])

    return "\n".join(lines)


def clear_community_improvement(state: Dict[str, Any]) -> None:
    """Archive completed improvement to history and clear current state."""
    ci = state.get("community_improvement")
    if ci is None:
        return

    # Archive to history
    history = state.setdefault("community_improvement_history", [])
    archive = {
        "task_id": ci.get("task_id"),
        "task_type": ci.get("task_type"),
        "description": ci.get("description"),
        "status": ci.get("status"),
        "post_id": ci.get("post_id"),
        "pr_url": ci.get("pr_url"),
        "fallback_used": ci.get("fallback_used", False),
        "selected_author": (ci.get("selected_comment") or {}).get("author"),
        "archived_at": int(time.time()),
    }
    history.append(archive)

    # Cap history
    if len(history) > MAX_COMMUNITY_HISTORY:
        state["community_improvement_history"] = history[-MAX_COMMUNITY_HISTORY:]

    state["community_improvement"] = None
    log.info("[community] Archived improvement: %s", archive.get("task_id"))
