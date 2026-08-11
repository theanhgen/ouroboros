"""Long-term improvement backlog -- persistent task queue across cycles."""

import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .model_defaults import DEFAULT_OPENAI_MODEL
from .storage import load_json_file, save_json_file, update_json_file

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


def _update_backlog(repo_root: Path, mutate) -> Any:
    """Run mutate over the item list under one lock.

    load_backlog then save_backlog as separate calls lets a second process
    interleave: both read version N, each appends its own item, and the later
    write drops the earlier one with no error anywhere.
    """
    def _apply(data: Any) -> Any:
        # load_backlog accepts an older bare-list file, so the mutators have to
        # as well; calling .get on one raised AttributeError.
        if isinstance(data, list):
            items = data
        else:
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                items = []
                data["items"] = items
        return mutate(items)

    return update_json_file(
        _backlog_path(repo_root), _apply, default={"items": []}
    )


def add_item(
    repo_root: Path,
    task_type: str,
    description: str,
    priority: int = 5,
    source: str = "auto",
) -> Dict[str, Any]:
    def _add(items: List[Dict[str, Any]]) -> Dict[str, Any]:
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
        return entry

    return _update_backlog(repo_root, _add)


def mark_done(repo_root: Path, item_id: str) -> None:
    def _mark(items: List[Dict[str, Any]]) -> None:
        for item in items:
            if item.get("id") == item_id:
                item["status"] = "done"
                item["completed_at"] = time.time()
                break

    _update_backlog(repo_root, _mark)


def mark_failed(repo_root: Path, item_id: str) -> None:
    def _mark(items: List[Dict[str, Any]]) -> None:
        for item in items:
            if item.get("id") == item_id:
                item["attempts"] = item.get("attempts", 0) + 1
                if item["attempts"] >= 3:
                    item["status"] = "abandoned"
                break

    _update_backlog(repo_root, _mark)


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


def _valid_entry_fields(entry: Dict[str, Any]) -> bool:
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


def _valid_organizer_fields(sections: Dict[str, List[Dict[str, Any]]]) -> bool:
    return all(
        _valid_entry_fields(entry)
        for name in ("keep", "merge")
        for entry in sections[name]
    )


@dataclass
class OrganizeResult:
    """What organize_backlog did, so a caller can report it."""

    ok: bool
    reason: str = ""
    kept: int = 0
    deleted: int = 0
    merged: int = 0

    def summary(self) -> str:
        if not self.ok:
            return f"Backlog unchanged: {self.reason}"
        return (
            f"Backlog organized: {self.kept} kept, {self.deleted} deleted, "
            f"{self.merged} merged"
        )


_ORGANIZER_SYSTEM_PROMPT = (
    "You are a Backlog Manager. Clean up a list of pending tasks.\n\n"
    "Decide an explicit outcome for EVERY task id you are given. Reply with "
    "JSON:\n"
    "{\n"
    '  "keep":   [{"id": "abc", "priority": 8, "desc": "...", "type": "..."}],\n'
    '  "delete": [{"id": "def", "reason": "duplicate of abc"}],\n'
    '  "merge":  [{"sources": ["ghi", "jkl"], "desc": "...", '
    '"type": "refactor", "priority": 7}]\n'
    "}\n\n"
    "Rules:\n"
    "- Every id in the input MUST appear exactly once, in keep, in delete, or "
    "in one merge's sources. A response that misses an id is rejected whole.\n"
    "- In 'keep', only include a field you are changing; omit the rest.\n"
    "- 'merge' replaces its sources with one new task, so 'desc' is required.\n"
    "- Delete only genuine duplicates or obsolete tasks, and say why."
)


def _validate_organizer_response(
    data: Any, reviewed: set
) -> "tuple[Optional[Dict[str, Any]], str]":
    """Check a response accounts for every id exactly once.

    The previous protocol expressed deletion by omission, which made a
    truncated reply indistinguishable from a deliberate prune: a model that
    echoed three of ten tasks silently deleted the other seven. Requiring an
    explicit outcome per id turns that into a validation failure.
    """
    if not isinstance(data, dict):
        return None, "response is not an object"

    sections = {}
    for name in ("keep", "delete", "merge"):
        value = data.get(name, [])
        if value is None:
            value = []
        if not isinstance(value, list):
            return None, f"'{name}' is not a list"
        if not all(isinstance(entry, dict) for entry in value):
            return None, f"'{name}' contains a non-object entry"
        sections[name] = value

    seen: Dict[str, str] = {}
    for name in ("keep", "delete"):
        for entry in sections[name]:
            item_id = entry.get("id")
            if not item_id:
                return None, f"a '{name}' entry has no id"
            if item_id in seen:
                return None, f"{item_id} appears in both {seen[item_id]} and {name}"
            seen[item_id] = name

    for entry in sections["merge"]:
        sources = entry.get("sources")
        if not isinstance(sources, list) or len(set(sources)) < 2:
            # A one-source "merge" combines nothing; it would just replace the
            # task with a fresh id and attempts back to zero, slipping past the
            # three-attempt abandonment rule.
            return None, "a 'merge' entry needs at least two distinct sources"
        if not str(entry.get("desc") or "").strip():
            return None, "a 'merge' entry has no description"
        for item_id in sources:
            if item_id in seen:
                return None, f"{item_id} appears in both {seen[item_id]} and merge"
            seen[item_id] = "merge"

    unknown = set(seen) - reviewed
    if unknown:
        return None, f"unknown ids: {', '.join(sorted(unknown))}"

    missing = reviewed - set(seen)
    if missing:
        # The whole point: an incomplete reply is a failure, not a deletion.
        return None, f"no decision for: {', '.join(sorted(missing))}"

    if not _valid_organizer_fields(sections):
        return None, "a field value is out of range or the wrong type"

    return sections, ""


def organize_backlog(
    repo_root: Path, client: Any, model: str = DEFAULT_OPENAI_MODEL
) -> OrganizeResult:
    """Use an LLM to prune, merge, and prioritize the backlog."""
    items = load_backlog(repo_root)
    pending = [i for i in items if i.get("status") == "pending"]
    if not pending:
        # Returning only when `items` is empty still sent "[]" to the model
        # for a backlog of nothing but done and failed records, and anything
        # it invented was persisted as new pending work.
        return OrganizeResult(ok=True, reason="nothing pending")

    # Records with no id cannot be referred to in the response, so they cannot
    # be given an outcome. They are left untouched rather than blocking the
    # whole run.
    # Across every record, not just the pending ones. Reconciliation removes
    # by id, so a pending task sharing an id with a done one would take the
    # completed record with it. add_item takes an unchecked eight-character
    # uuid prefix, so a collision is unlikely rather than impossible.
    all_ids = [i["id"] for i in items if i.get("id")]
    duplicates = {i for i in all_ids if all_ids.count(i) > 1}
    if duplicates:
        return OrganizeResult(
            ok=False,
            reason=f"duplicate backlog ids: {', '.join(sorted(duplicates))}",
        )

    addressable = [i for i in pending if i.get("id")]

    pending_by_id = {i["id"]: i for i in addressable}
    if not pending_by_id:
        return OrganizeResult(ok=True, reason="no identifiable pending tasks")

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
        for i in pending_by_id.values()
    ], indent=2)

    user = f"Current Pending Backlog:\n{backlog_text}\n\nPlease organize this backlog."

    from .llm import chat_completion

    try:
        # Inside the try: this function's contract is to log and leave the
        # backlog alone on failure. chat_completion happens to swallow its own
        # errors today, so the call only looked safe out here.
        content, _ = chat_completion(
            client, _ORGANIZER_SYSTEM_PROMPT, user, model=model
        )
        if "{" in content:
            content = content[content.find("{"):content.rfind("}") + 1]
        data = json.loads(content)
    except Exception as exc:
        log.warning("Backlog organizer call failed: %s", exc)
        return OrganizeResult(ok=False, reason=str(exc))

    reviewed = set(pending_by_id)
    sections, error = _validate_organizer_response(data, reviewed)
    if sections is None:
        log.warning("Backlog organizer response rejected: %s", error)
        return OrganizeResult(ok=False, reason=error)

    result = OrganizeResult(
        ok=True,
        kept=len(sections["keep"]),
        deleted=len(sections["delete"]),
        merged=len(sections["merge"]),
    )

    def _apply(current: List[Dict[str, Any]]) -> None:
        """Reconcile against the file as it is now, not the snapshot.

        The model call above takes seconds to minutes. Anything committed in
        the meantime is live state and wins: the organizer's view of those
        records is stale by definition.
        """
        def _still_pending(item: Dict[str, Any]) -> bool:
            return item.get("status") == "pending"

        def _semantically_unchanged(item: Dict[str, Any]) -> bool:
            """Do the fields the organizer reasoned about still match?

            attempts and completed_at are bookkeeping and may move freely. But
            if a concurrent keep changed the description or type, a task the
            model called a duplicate may no longer be one, and a destructive
            decision about it is stale.
            """
            snapshot = pending_by_id.get(item.get("id"))
            if snapshot is None:
                return False
            return all(
                item.get(field) == snapshot.get(field)
                for field in ("task_type", "description", "priority")
            )

        def _removable(item_id: Any) -> bool:
            live = live_now.get(item_id)
            return (
                live is not None
                and item_id in reviewed
                and _still_pending(live)
                and _semantically_unchanged(live)
            )

        live_now = {i.get("id"): i for i in current if i.get("id")}

        drop_ids = {e["id"] for e in sections["delete"] if _removable(e["id"])}

        # A merge is all-or-nothing. Removing only the sources that survived
        # would leave the completed one in place and add a replacement that
        # duplicates it.
        applied_merges = [
            entry for entry in sections["merge"]
            if all(_removable(sid) for sid in entry["sources"])
        ]
        for entry in applied_merges:
            drop_ids.update(entry["sources"])

        kept = [item for item in current if item.get("id") not in drop_ids]
        live_by_id = {i.get("id"): i for i in kept if i.get("id")}
        live_ids = set(live_by_id)

        for entry in sections["keep"]:
            live = live_by_id.get(entry["id"])
            if live is None or not _still_pending(live):
                # Removed or finished while the model ran. Not ours.
                continue
            if not _semantically_unchanged(live):
                # Another writer rewrote it. The organizer's version of these
                # fields is stale, and applying it would undo that edit.
                # A concurrent mark_failed still passes: attempts is not one of
                # the fields compared.
                continue
            # Field level, not record level: a concurrent mark_failed bumps
            # attempts without changing anything the organizer decides, so its
            # priority or wording change should still apply. Bookkeeping
            # fields -- attempts, status, created_at -- are never written here.
            for key, field in (("type", "task_type"), ("desc", "description"),
                               ("priority", "priority")):
                value = entry.get(key)
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    # A blank string is an omission, not an instruction to
                    # clear a field nothing else can recover.
                    continue
                live[field] = value

        for entry in applied_merges:
            item_id = str(uuid.uuid4())[:8]
            while item_id in live_ids:
                item_id = str(uuid.uuid4())[:8]
            live_ids.add(item_id)
            kept.append({
                "id": item_id,
                "task_type": entry.get("type") or "refactor",
                "description": entry["desc"],
                "priority": entry.get("priority") or 5,
                # Never model-controlled.
                "status": "pending",
                "source": "backlog_organizer",
                "created_at": time.time(),
                "attempts": 0,
            })

        current[:] = kept

    try:
        _update_backlog(repo_root, _apply)
    except Exception as exc:
        log.exception("Backlog organization failed")
        return OrganizeResult(ok=False, reason=str(exc))

    log.info("%s", result.summary())
    return result
