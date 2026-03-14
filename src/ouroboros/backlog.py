"""Long-term improvement backlog -- persistent task queue across cycles."""

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

BACKLOG_FILE = "config/backlog.json"


def _backlog_path(repo_root: Path) -> Path:
    return repo_root / BACKLOG_FILE


def load_backlog(repo_root: Path) -> List[Dict[str, Any]]:
    path = _backlog_path(repo_root)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("items", []) if isinstance(data, dict) else data
    except (json.JSONDecodeError, KeyError):
        log.warning("Corrupt backlog file, returning empty")
        return []


def save_backlog(repo_root: Path, items: List[Dict[str, Any]]) -> None:
    path = _backlog_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, indent=2)
    import os
    os.replace(tmp, str(path))


def add_item(
    repo_root: Path,
    task_type: str,
    description: str,
    priority: int = 5,
    source: str = "auto",
) -> Dict[str, Any]:
    items = load_backlog(repo_root)
    # Dedup by description similarity
    for item in items:
        if item.get("description") == description:
            return item

    entry = {
        "id": str(uuid.uuid4())[:8],
        "task_type": task_type,
        "description": description,
        "priority": max(1, min(10, priority)),
        "status": "pending",
        "source": source,
        "created_at": time.time(),
        "attempts": 0,
    }
    items.append(entry)
    save_backlog(repo_root, items)
    return entry


def mark_done(repo_root: Path, item_id: str) -> None:
    items = load_backlog(repo_root)
    for item in items:
        if item.get("id") == item_id:
            item["status"] = "done"
            item["completed_at"] = time.time()
            break
    save_backlog(repo_root, items)


def mark_failed(repo_root: Path, item_id: str) -> None:
    items = load_backlog(repo_root)
    for item in items:
        if item.get("id") == item_id:
            item["attempts"] = item.get("attempts", 0) + 1
            if item["attempts"] >= 3:
                item["status"] = "abandoned"
            break
    save_backlog(repo_root, items)


def get_pending(repo_root: Path) -> List[Dict[str, Any]]:
    items = load_backlog(repo_root)
    pending = [i for i in items if i.get("status") == "pending"]
    return sorted(pending, key=lambda x: x.get("priority", 5), reverse=True)


def format_backlog_for_llm(items: List[Dict[str, Any]]) -> str:
    pending = [i for i in items if i.get("status") == "pending"]
    if not pending:
        return ""
    top = sorted(pending, key=lambda x: x.get("priority", 5), reverse=True)[:5]
    lines = ["### Improvement Backlog (prioritize these if applicable)"]
    for item in top:
        lines.append(
            f"- [P{item.get('priority', 5)}] {item.get('task_type', '?')}: "
            f"{item.get('description', '')} (attempts: {item.get('attempts', 0)})"
        )
    return "\n".join(lines)
