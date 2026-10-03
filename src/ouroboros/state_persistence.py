"""State persistence utilities for Ouroboros."""

from pathlib import Path

def _state_path(repo_root: Path, filename: str) -> Path:
    """Resolve state file path under the repo root.

    Returns a Path for the given filename located directly under the repository root.
    """
    return repo_root / filename
