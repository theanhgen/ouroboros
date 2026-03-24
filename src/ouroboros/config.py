from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class SafetyConfig:
    pr_only: bool = True
    allow_network: bool = True
    allow_write_default_branch: bool = False
    require_human_approval: bool = True
    allow_self_modification: bool = True
    enable_auto_merge: bool = True  # Auto-merge PRs when checks pass
    max_retry_on_failure: int = 1  # Retry with root cause analysis on test regression

    # Sandbox configuration
    sandbox_enabled: bool = False
    sandbox_image: str = "python:3.11-slim"

    # Multi-model review
    reviewer_model: str = "claude-3-5-sonnet-20240620"

    # Self-improvement limits
    max_improvements_per_day: int = 3
    max_changed_files_per_pr: int = 3
    max_lines_changed_per_pr: int = 200

    # Path restrictions for the improvement engine
    allowed_modification_paths: Tuple[str, ...] = (
        "src/ouroboros/",
        "tests/",
        "docs/wiki/",
    )
    forbidden_modification_paths: Tuple[str, ...] = (
        "config.py",
        "improvement.py",
        "git_ops.py",
        "evaluation.py",
        "policies.py",
    )
