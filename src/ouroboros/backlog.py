"""Long-term improvement backlog -- persistent task queue across cycles."""

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .model_defaults import DEFAULT_OPENAI_MODEL
from .storage import load_json_file, save_json_file

log = logging.getLogger(__name__)

BACKLOG_FILE = "config/backlog.json"


def _backlog_path(repo_root: Path) -> Path:
    return repo_root / BACKLOG_FILE


def load_backlog(repo_root: Path) -> List[Dict[str, Any]]:
    data = load_json_file(
        _backlog_path(repo_root),
        default={"items": []},
        error_msg="Corrupt backlog file, returning empty",
        logger=log,
    )
    if isinstance(data, dict):
        items = data.get("items")
    else:
        items = data
    # A null or non-list payload is corruption, not an empty backlog's shape;
    # returning it would break the annotated contract for every caller.
    return items if isinstance(items, list) else []


def save_backlog(repo_root: Path, items: List[Dict[str, Any]]) -> None:
    save_json_file(_backlog_path(repo_root), {"items": items})


def add_item(
    repo_root: Path,
    task_type: str,
    description: str,
    priority: int = 5,
    source: str = "auto",
) -> Dict[str, Any]:
    items = load_backlog(repo_root)
    # Dedup pending items by description similarity
    for item in items:
        if item.get("description") == description and item.get("status") == "pending":
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


def organize_backlog(repo_root: Path, client: Any, model: str = DEFAULT_OPENAI_MODEL) -> None:
    """Use an LLM to prune, merge, and prioritize the backlog."""
    items = load_backlog(repo_root)
    if not items:
        return

    backlog_text = json.dumps([
        {"id": i["id"], "type": i["task_type"], "desc": i["description"], "priority": i["priority"]}
        for i in items if i["status"] == "pending"
    ], indent=2)

    system = (
        "You are a Backlog Manager. Your goal is to clean up a list of pending tasks.\n"
        "1. Identify duplicates and merge them.\n"
        "2. Group small related bugs into a single 'refactor' task if they share a file.\n"
        "3. Update priorities (1-10) based on perceived impact.\n\n"
        "Output JSON with a single key 'items', a list of updated task objects."
    )
    
    user = f"Current Pending Backlog:\n{backlog_text}\n\nPlease organize this backlog."
    
    from .llm import chat_completion
    content, _ = chat_completion(client, system, user, model=model)
    
    try:
        if "{" in content:
            content = content[content.find("{"):content.rfind("}")+1]
        data = json.loads(content)
        new_items = data.get("items", [])
        
        # Merge new items back with non-pending ones
        final_items = [i for i in items if i["status"] != "pending"]
        for ni in new_items:
            # Check if this was an existing ID
            existing = [i for i in items if i["id"] == ni.get("id")]
            if existing:
                e = existing[0]
                e["task_type"] = ni.get("type", e["task_type"])
                e["description"] = ni.get("desc", e["description"])
                e["priority"] = ni.get("priority", e["priority"])
                final_items.append(e)
            else:
                # New "Epic" or merged task
                final_items.append({
                    "id": ni.get("id") or str(uuid.uuid4())[:8],
                    "task_type": ni.get("type", "refactor"),
                    "description": ni.get("desc", ""),
                    "priority": ni.get("priority", 5),
                    "status": ni.get("status", "pending"),
                    "source": "backlog_organizer",
                    "created_at": time.time(),
                    "attempts": 0
                })
        
        save_backlog(repo_root, final_items)
        log.info("Backlog organized semantically.")
    except Exception:
        log.exception("Backlog organization failed")
