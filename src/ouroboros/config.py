from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class SafetyConfig:
    """Configuration for the safety/policy layer of the self-improvement engine.

    Notes on require_human_approval:
        This flag means "require a human reviewer to approve the PR before merge"
        and only makes sense when CODEOWNERS or branch protection rules are configured
        on the repository.  In the default autonomous setup there are no such rules,
        so the field defaults to False to match the actual enable_auto_merge=True
        behavior.  Set it to True only when you have branch protection in place.
    """

    pr_only: bool = True
    allow_network: bool = True
    allow_write_default_branch: bool = False
    # See class docstring -- False matches the default enable_auto_merge=True behavior.
    require_human_approval: bool = False
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
