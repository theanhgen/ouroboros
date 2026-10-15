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

def trim_json_list(repo_root: Path, filename: str, max_size: int, list_key: Optional[str] = None) -> None:
    """Ensure that the list stored in a JSON file is bounded by `max_size`.

    The function reads the JSON file, locates the list (either at the root of the
    JSON document or under the given `list_key`), and trims it to at most `max_size`
    elements while preserving order. If trimming was necessary, the file is updated
    atomically.

    Args:
        repo_root: The repository root path.
        filename: Repository‑relative path to the JSON file.
        max_size: Maximum allowed number of elements in the list.
        list_key: Optional key in the JSON object where the list is stored.
            If `None`, the JSON file itself is expected to be a list.

    Returns:
        None

    Raises:
        TypeError: If the data type does not match the expectation (e.g., expecting
            a JSON object when `list_key` is provided but the file contains a list).
    """
    data = load_json_file(repo_root, filename)
    if data is None:
        # File does not exist or cannot be parsed; nothing to trim.
        return

    if list_key is not None:
        if not isinstance(data, dict):
            raise TypeError(
                f"Expected a JSON object in {filename} when list_key is provided"
            )
        lst = data.get(list_key)
        if lst is None or not isinstance(lst, list):
            # No list present; nothing to trim.
            return
        trimmed = trim_list(lst, max_size)
        if trimmed != lst:
            data[list_key] = trimmed
            write_json_file(repo_root, filename, data)
    else:
        if not isinstance(data, list):
            raise TypeError(
                f"Expected a list in {filename} when list_key is None"
            )
        trimmed = trim_list(data, max_size)
        if trimmed != data:
            write_json_file(repo_root, filename, trimmed)
