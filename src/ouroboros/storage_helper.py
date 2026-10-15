"""Shared utilities for storing data in the repository."""
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


def _resolve_repo_path(repo_root: Path, *parts: str) -> Path:
    """Resolve a repository‑relative path."""
    return repo_root.joinpath(*parts)


def _ensure_parent_dirs(path: Path) -> None:
    """Ensure the parent directory of `path` exists."""
    path.parent.mkdir(parents=True, exist_ok=True)


def _atomic_write_content(path: Path, content: str) -> None:
    """Atomically write `content` to `path` using a temporary file."""
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as tmp:
            temp_path = Path(tmp.name)
            tmp.write(content)
        os.replace(temp_path, path)
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        raise


def load_json_file(repo_root: Path, filename: str, default: Any = None) -> Any:
    """Load JSON data from a repository‑relative file.

    If the file does not exist or cannot be parsed, `default` is returned.
    Parent directories are ensured to exist.
    """
    path = _resolve_repo_path(repo_root, filename)
    _ensure_parent_dirs(path)
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Failed to load JSON file %s: %s", path, e)
        return default


def write_json_file(repo_root: Path, filename: str, data: Any) -> Path:
    """Write `data` as JSON to a repository‑relative file atomically.

    Ensures parent directories exist and performs an atomic write.
    Returns the final path.
    """
    path = _resolve_repo_path(repo_root, filename)
    _ensure_parent_dirs(path)
    content = json.dumps(data, ensure_ascii=False, indent=2)
    _atomic_write_content(path, content)
    return path

def trim_list(lst: list[Any], max_size: int) -> list[Any]:
    """Trim a list to a maximum size while preserving order.

    Returns a new list containing the first `max_size` elements.
    If `lst` is shorter than or equal to `max_size`, it is returned unchanged.
    """
    if max_size < 0:
        raise ValueError("max_size must be non-negative")
    return lst[:max_size]
