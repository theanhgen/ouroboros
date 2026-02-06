from dataclasses import dataclass
from pathlib import Path
from typing import List

from .config import SafetyConfig


@dataclass(frozen=True)
class Evidence:
    source: str
    location: str
    note: str


class PolicyError(RuntimeError):
    pass


def require_pr_only(is_pr_only: bool) -> None:
    if not is_pr_only:
        raise PolicyError("PR-only policy violated")


def validate_modification_scope(
    file_paths: List[str],
    config: SafetyConfig | None = None,
) -> List[str]:
    """Validate that all file paths are within allowed modification scope.

    Returns a list of violation messages (empty = all paths valid).
    """
    config = config or SafetyConfig()
    violations = []

    for file_path in file_paths:
        basename = Path(file_path).name

        # Check forbidden files
        if basename in config.forbidden_modification_paths:
            violations.append(f"Forbidden file: {file_path} ({basename} is immutable)")
            continue

        # Check allowed path prefixes
        allowed = any(
            file_path.startswith(prefix)
            for prefix in config.allowed_modification_paths
        )
        if not allowed:
            violations.append(
                f"Out of scope: {file_path} (must be under {config.allowed_modification_paths})"
            )

    return violations


def validate_change_size(
    num_files: int,
    num_lines: int,
    config: SafetyConfig | None = None,
) -> List[str]:
    """Validate that a change doesn't exceed size limits.

    Returns a list of violation messages (empty = valid).
    """
    config = config or SafetyConfig()
    violations = []

    if num_files > config.max_changed_files_per_pr:
        violations.append(
            f"Too many files: {num_files} > {config.max_changed_files_per_pr}"
        )
    if num_lines > config.max_lines_changed_per_pr:
        violations.append(
            f"Too many lines: {num_lines} > {config.max_lines_changed_per_pr}"
        )

    return violations
