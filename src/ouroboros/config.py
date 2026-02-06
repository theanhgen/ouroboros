from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class SafetyConfig:
    pr_only: bool = True
    allow_network: bool = True
    allow_write_default_branch: bool = False
    require_human_approval: bool = True
    allow_self_modification: bool = True

    # Self-improvement limits
    max_improvements_per_day: int = 3
    max_changed_files_per_pr: int = 3
    max_lines_changed_per_pr: int = 200

    # Path restrictions for the improvement engine
    allowed_modification_paths: Tuple[str, ...] = (
        "src/ouroboros/",
        "tests/",
    )
    forbidden_modification_paths: Tuple[str, ...] = (
        "config.py",
        "improvement.py",
        "git_ops.py",
        "evaluation.py",
        "policies.py",
    )
