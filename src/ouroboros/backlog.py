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


def _valid_organizer_fields(entry: Dict[str, Any]) -> bool:
    """Check the field values an organizer entry may set.

    A null or wrongly typed value here is written straight into the queue: a
    null priority makes get_pending raise while sorting, and a blank
    description leaves a task nobody can act on.
    """
    task_type = entry.get("type")
    if task_type is not None and (not isinstance(task_type, str) or not task_type.strip()):
        return False

    desc = entry.get("desc")
    if desc is not None and not isinstance(desc, str):
        return False

    priority = entry.get("priority")
    if priority is not None:
        if isinstance(priority, bool) or not isinstance(priority, int):
            return False
        if not 1 <= priority <= 10:
            return False

    return True


def organize_backlog(repo_root: Path, client: Any, model: str = DEFAULT_OPENAI_MODEL) -> None:
    """Use an LLM to prune, merge, and prioritize the backlog."""
    items = load_backlog(repo_root)
    pending = [i for i in items if i.get("status") == "pending"]
    if not pending:
        # Returning only when `items` is empty still sent "[]" to the model
        # for a backlog of nothing but done and failed records, and anything
        # it invented was persisted as new pending work.
        return

    # .get throughout: a hand-edited or legacy record missing a key would
    # otherwise raise out of this function, which is supposed to log and leave
    # the backlog alone.
    backlog_text = json.dumps([
        {
            "id": i.get("id"),
            "type": i.get("task_type"),
            "desc": i.get("description"),
            "priority": i.get("priority"),
        }
        for i in pending
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

    try:
        # Inside the try: this function's contract is to log and leave the
        # backlog alone on failure. chat_completion happens to swallow its own
        # errors today, so the call only looked safe out here.
        content, _ = chat_completion(client, system, user, model=model)
        if "{" in content:
            content = content[content.find("{"):content.rfind("}")+1]
        data = json.loads(content)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            log.warning("Backlog organizer returned no usable 'items'; leaving backlog alone")
            return
        new_items = data["items"]
        if not new_items:
            # Omission is how this protocol expresses pruning, so an empty
            # list is indistinguishable from a truncated reply -- and it would
            # delete every pending item. Refuse rather than guess.
            log.warning("Backlog organizer returned an empty list; leaving backlog alone")
            return

        # Updates resolve against pending records only. Searching every record
        # let an invented id collide with a done or failed one, mutating
        # history in place and appending it twice.
        pending_by_id = {i["id"]: i for i in pending if i.get("id")}
        all_ids = {i["id"] for i in items if i.get("id")}

        # Validate the whole response before touching anything, so a single
        # bad entry cannot leave the backlog half-rewritten.
        seen_ids = set()
        for ni in new_items:
            if not isinstance(ni, dict):
                log.warning("Backlog organizer returned a non-object entry; leaving backlog alone")
                return

            item_id = ni.get("id")
            if item_id is not None:
                if item_id in seen_ids:
                    # mark_done stops at the first match, so a duplicate would
                    # stay pending and be worked twice.
                    log.warning("Backlog organizer repeated id %r; leaving backlog alone", item_id)
                    return
                seen_ids.add(item_id)

            if item_id not in pending_by_id and not str(ni.get("desc") or "").strip():
                # Neither an update to something real nor a usable new task.
                log.warning("Backlog organizer returned an unusable entry (%r); leaving backlog alone", ni)
                return

            if not _valid_organizer_fields(ni):
                log.warning("Backlog organizer returned bad field values (%r); leaving backlog alone", ni)
                return

        final_items = [i for i in items if i.get("status") != "pending"]
        for ni in new_items:
            existing = pending_by_id.get(ni.get("id"))
            if existing is not None:
                # Only overwrite what the model actually supplied; an explicit
                # null is an omission, not an instruction to blank the field.
                for key, field in (("type", "task_type"), ("desc", "description"),
                                   ("priority", "priority")):
                    value = ni.get(key)
                    if value is None:
                        continue
                    if isinstance(value, str) and not value.strip():
                        # A blank string is an omission too. Applying it would
                        # leave a task with no description, which nobody can
                        # act on and nothing else can recover.
                        continue
                    existing[field] = value
                final_items.append(existing)
            else:
                # A fresh id when the model's collides with any existing
                # record, so a new task cannot overwrite history.
                item_id = ni.get("id")
                if not item_id or item_id in all_ids:
                    item_id = str(uuid.uuid4())[:8]
                all_ids.add(item_id)
                final_items.append({
                    "id": item_id,
                    "task_type": ni.get("type") or "refactor",
                    "description": ni.get("desc", ""),
                    "priority": ni.get("priority") or 5,
                    # Never model-controlled: status="done" on a new entry
                    # would delete the omitted records and leave nothing
                    # pending to replace them.
                    "status": "pending",
                    "source": "backlog_organizer",
                    "created_at": time.time(),
                    "attempts": 0,
                })

        save_backlog(repo_root, final_items)
        log.info("Backlog organized semantically.")
    except Exception:
        log.exception("Backlog organization failed")
