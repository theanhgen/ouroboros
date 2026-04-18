"""Wiki writer -- Ouroboros maintains its own documentation."""

import json
import logging
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

WIKI_DIR = "docs/wiki"


def _wiki_path(repo_root: Path) -> Path:
    p = repo_root / WIKI_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_page(repo_root: Path, filename: str, content: str) -> Path:
    path = _wiki_path(repo_root) / filename
    path.write_text(content, encoding="utf-8")
    return path


def generate_architecture_page(repo_root: Path) -> str:
    """Generate the architecture overview from actual source files."""
    from .codebase import list_source_files, get_function_signatures

    src_dir = repo_root / "src" / "ouroboros"
    modules = {}
    if src_dir.exists():
        for py in sorted(src_dir.glob("*.py")):
            if py.name.startswith("__"):
                continue
            try:
                sigs = get_function_signatures(py)
                line_count = len(py.read_text(encoding="utf-8").splitlines())
                # sigs may be a list or a string
                if isinstance(sigs, list):
                    sigs = "\n".join(str(s) for s in sigs)
                modules[py.name] = {"lines": line_count, "functions": sigs or ""}
            except Exception:
                modules[py.name] = {"lines": 0, "functions": ""}

    lines = [
        "# Architecture",
        "",
        "Auto-generated overview of the Ouroboros codebase.",
        f"Last updated: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        "",
        "## Modules",
        "",
    ]

    for mod, info in sorted(modules.items()):
        lines.append(f"### `{mod}` ({info['lines']} lines)")
        funcs = info["functions"]
        if funcs:
            for sig in funcs.strip().split("\n"):
                sig = sig.strip()
                if not sig:
                    continue
                # Parse dict-like signature into readable format
                if sig.startswith("{"):
                    try:
                        import ast
                        d = ast.literal_eval(sig)
                        name = d.get("name", "?")
                        args = ", ".join(d.get("args", []))
                        sig = f"{name}({args})"
                    except Exception:
                        pass
                lines.append(f"- `{sig}`")
        lines.append("")

    return "\n".join(lines)


def generate_metrics_page(repo_root: Path) -> str:
    """Generate the metrics/stats page from real data."""
    from .evaluation import load_history
    from .metrics import load_metrics

    history = load_history(repo_root)
    snapshots = load_metrics(repo_root)

    lines = [
        "# Metrics",
        "",
        f"Last updated: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        "",
    ]

    # Overall stats
    total = len(history)
    merged = sum(1 for r in history if r.outcome == "merged")
    success = sum(1 for r in history if r.outcome == "success")
    reverted = sum(1 for r in history if r.outcome == "reverted")
    failed = sum(1 for r in history if r.outcome == "failed")

    lines.extend([
        "## Overall",
        "",
        f"- Total improvement attempts: {total}",
        f"- Merged: {merged}",
        f"- Success (pending merge): {success}",
        f"- Reverted: {reverted}",
        f"- Failed: {failed}",
        f"- Success rate: {int(100 * (merged + success) / total) if total else 0}%",
        "",
    ])

    # Per task type
    attempts = Counter()
    successes = Counter()
    reverts = Counter()
    for r in history:
        attempts[r.task_type] += 1
        if r.outcome in ("merged", "success"):
            successes[r.task_type] += 1
        elif r.outcome == "reverted":
            reverts[r.task_type] += 1

    if attempts:
        lines.extend(["## By Task Type", "", "| Type | Attempts | Succeeded | Reverted | Success Rate |", "|------|----------|-----------|----------|-------------|"])
        for tt, count in attempts.most_common():
            s = successes.get(tt, 0)
            rv = reverts.get(tt, 0)
            rate = int(100 * s / count) if count else 0
            lines.append(f"| {tt} | {count} | {s} | {rv} | {rate}% |")
        lines.append("")

    # Trend from metrics snapshots
    if snapshots:
        latest = snapshots[-1]
        lines.extend([
            "## Latest Snapshot",
            "",
            f"- Source lines: {latest.get('src_lines', 0)}",
            f"- Test lines: {latest.get('test_lines', 0)}",
            f"- 30-day success rate: {latest.get('success_rate_30d', 0)}%",
            "",
        ])

        # Weekly trend
        if len(snapshots) >= 2:
            week_ago = time.time() - 7 * 86400
            older = [s for s in snapshots if s.get("timestamp", 0) < week_ago]
            if older:
                prev = older[-1]
                lines.extend([
                    "## 7-Day Trend",
                    "",
                    f"- Source: {latest.get('src_lines', 0) - prev.get('src_lines', 0):+d} lines",
                    f"- Tests: {latest.get('test_lines', 0) - prev.get('test_lines', 0):+d} lines",
                    f"- Success rate: {latest.get('success_rate_30d', 0) - prev.get('success_rate_30d', 0):+.1f}%",
                    "",
                ])

    return "\n".join(lines)


def generate_changelog_page(repo_root: Path) -> str:
    """Generate changelog from improvement history."""
    from .evaluation import load_history

    history = load_history(repo_root)

    lines = [
        "# Changelog",
        "",
        "Autonomous improvements made by Ouroboros, newest first.",
        f"Last updated: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        "",
    ]

    # Group by date
    by_date: Dict[str, List] = {}
    for r in reversed(history):
        date = time.strftime("%Y-%m-%d", time.localtime(r.timestamp)) if r.timestamp else "unknown"
        by_date.setdefault(date, []).append(r)

    for date, records in list(by_date.items())[:30]:  # Last 30 days
        lines.append(f"## {date}")
        lines.append("")
        for r in records:
            status_icon = {"merged": "[MERGED]", "success": "[OK]", "reverted": "[REVERTED]", "failed": "[FAILED]"}.get(r.outcome, f"[{r.outcome.upper()}]")
            line = f"- {status_icon} **{r.task_type}**: {r.description}"
            if r.pr_url:
                line += f" ([PR]({r.pr_url}))"
            lines.append(line)

            # Test delta
            before = r.test_delta.get("before", {})
            after = r.test_delta.get("after", {})
            if before or after:
                bp = before.get("passed", 0)
                bf = before.get("failed", 0)
                ap = after.get("passed", 0)
                af = after.get("failed", 0)
                lines.append(f"  - Tests: {bp}p/{bf}f -> {ap}p/{af}f")
        lines.append("")

    return "\n".join(lines)


def generate_config_page(repo_root: Path) -> str:
    """Generate configuration reference from actual config."""
    from .config import SafetyConfig
    from .moltbook import RunnerConfig

    lines = [
        "# Configuration Reference",
        "",
        f"Last updated: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        "",
        "## Safety Config (`src/ouroboros/config.py`)",
        "",
        "| Parameter | Default | Description |",
        "|-----------|---------|-------------|",
    ]

    safety = SafetyConfig()
    safety_docs = {
        "pr_only": "Only create PRs, never commit to default branch directly",
        "allow_network": "Allow network access during improvement",
        "allow_write_default_branch": "Allow writing directly to main/master",
        "require_human_approval": "Require human review before merge",
        "allow_self_modification": "Allow modifying own runtime config",
        "enable_auto_merge": "Auto-merge PRs when CI checks pass",
        "max_retry_on_failure": "Number of root-cause-analysis retries on test regression",
        "max_improvements_per_day": "Rate limit on daily improvement attempts",
        "max_changed_files_per_pr": "Max files per autonomous PR",
        "max_lines_changed_per_pr": "Max lines changed per autonomous PR",
        "allowed_modification_paths": "Directories the agent can modify",
        "forbidden_modification_paths": "Files that can never be modified",
    }
    for field_name, field_obj in SafetyConfig.__dataclass_fields__.items():
        val = getattr(safety, field_name)
        doc = safety_docs.get(field_name, "")
        lines.append(f"| `{field_name}` | `{val}` | {doc} |")

    lines.extend([
        "",
        "## Runtime Files",
        "",
        "The live runtime config does not come from the repo `config/` directory.",
        "",
        "| Path | Purpose |",
        "|------|---------|",
        "| `~/.config/moltbook/agent.json` | Live runner configuration |",
        "| `~/.config/moltbook/credentials.json` | Secrets and runtime credentials |",
        "| `~/.config/moltbook/state.json` | Moltbook loop state |",
        "| `~/.config/moltbook/self_improvement_state.json` | Scheduled self-improvement state |",
        "| `~/.config/moltbook/knowledge_base.json` | Persisted feed insights |",
        "",
        "`config/agent.json` in the repo is only a checked-in sample/reference file.",
        "",
        "## Credentials (`~/.config/moltbook/credentials.json`)",
        "",
        "Typical keys:",
        "",
        "| Key | Purpose |",
        "|-----|---------|",
        "| `openai_api_key` | Required for OpenAI-backed LLM features |",
        "| `api_key` | Moltbook API key for status/feed/posting |",
        "| `agent_name` | Required for Moltbook features that inspect the agent's own posts |",
        "| `telegram_bot_token` | Optional Telegram notifications secret |",
        "| `telegram_chat_id` | Optional Telegram destination |",
        "",
        "## Runner Config (`~/.config/moltbook/agent.json`)",
        "",
        "| Parameter | Default | Description |",
        "|-----------|---------|-------------|",
    ])

    runner = RunnerConfig()
    runner_docs = {
        "interval_seconds": "Main loop cycle interval",
        "enable_auto_comment": "Post comments on Moltbook posts",
        "dry_run": "Preview mode, no actual changes",
        "enable_telegram_notifications": "Send events to Telegram",
        "enable_self_improvement": "Enable autonomous code improvement",
        "improvement_interval_hours": "Hours between improvement attempts",
        "improvement_model": "LLM model for code generation",
        "enable_auto_merge": "Auto-merge PRs when checks pass",
        "enable_github_improvement": "Auto-fix GitHub issues",
        "github_improvement_interval_hours": "Hours between issue checks",
        "enable_community_improvement": "Post improvements for community feedback",
        "enable_auto_git_push": "Auto-commit and push state changes",
    }
    for field_name in RunnerConfig.__dataclass_fields__:
        val = getattr(runner, field_name)
        doc = runner_docs.get(field_name, "")
        if doc:  # Only document the important ones
            display_val = str(val) if not isinstance(val, str) else val
            if len(display_val) > 40:
                display_val = display_val[:37] + "..."
            lines.append(f"| `{field_name}` | `{display_val}` | {doc} |")

    return "\n".join(lines)


def generate_failures_page(repo_root: Path) -> str:
    """Generate a page documenting failure patterns and lessons learned."""
    from .evaluation import load_history

    history = load_history(repo_root)
    failed = [r for r in history if r.outcome in ("failed", "reverted", "closed")]

    lines = [
        "# Failure Patterns",
        "",
        "What went wrong and what was learned. Auto-generated from improvement history.",
        f"Last updated: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        "",
    ]

    if not failed:
        lines.append("No failures recorded yet.")
        return "\n".join(lines)

    # Group by task type
    by_type: Dict[str, List] = {}
    for r in failed:
        by_type.setdefault(r.task_type, []).append(r)

    for tt, records in sorted(by_type.items(), key=lambda x: len(x[1]), reverse=True):
        lines.append(f"## {tt} ({len(records)} failures)")
        lines.append("")
        for r in records[-5:]:  # Last 5 per type
            lines.append(f"- **{r.outcome}**: {r.description}")
            if getattr(r, "details", ""):
                lines.append(f"  - Reason: {r.details[:200]}")
            if r.feedback:
                lines.append(f"  - Feedback: {r.feedback[:200]}")
        lines.append("")

    return "\n".join(lines)


def update_wiki(repo_root: Path) -> List[str]:
    """Regenerate all wiki pages. Returns list of updated file paths."""
    updated = []
    pages = [
        ("architecture.md", generate_architecture_page),
        ("metrics.md", generate_metrics_page),
        ("changelog.md", generate_changelog_page),
        ("configuration.md", generate_config_page),
        ("failures.md", generate_failures_page),
    ]

    for filename, generator in pages:
        try:
            content = generator(repo_root)
            path = _write_page(repo_root, filename, content)
            updated.append(str(path.relative_to(repo_root)))
            log.info("[wiki] Updated %s", filename)
        except Exception:
            log.exception("[wiki] Failed to generate %s", filename)

    # Generate index
    try:
        index = _generate_index(repo_root)
        path = _write_page(repo_root, "index.md", index)
        updated.append(str(path.relative_to(repo_root)))
    except Exception:
        log.exception("[wiki] Failed to generate index")

    return updated


def _generate_index(repo_root: Path) -> str:
    """Generate the wiki index page."""
    return f"""# Ouroboros Wiki

Auto-maintained documentation by an autonomous self-improving agent.
Last updated: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}

## Pages

- [Architecture](architecture.md) -- Module overview, function signatures, code structure
- [Metrics](metrics.md) -- Success rates, revert rates, LOC trends
- [Changelog](changelog.md) -- Every autonomous improvement, with test deltas
- [Configuration](configuration.md) -- All config parameters and their defaults
- [Failure Patterns](failures.md) -- What went wrong and what was learned

## About

Ouroboros is a self-improving autonomous agent running on a Raspberry Pi.
It continuously analyzes its own codebase, generates improvements, validates
them against its test suite, creates PRs, and auto-merges when checks pass.

This wiki is regenerated automatically. Every page reflects the current
state of the codebase, not a snapshot from when someone last wrote docs.
"""
