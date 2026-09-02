"""Knowledge base -- persisted insights from community posts."""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .storage import load_json_file, save_json_file

log = logging.getLogger(__name__)

KB_PATH = os.path.expanduser("~/.config/moltbook/knowledge_base.json")
KB_DEFAULT = {"entries": [], "summary_cache": "", "summary_updated_at": 0}

# Kept for the migration only. Entries live in SQLite now and are not capped:
# the cap existed because the whole list was rewritten as JSON on every append,
# so keeping more meant a bigger file to load each cycle.
MAX_ENTRIES = 200

# Completion marker: a non-empty table does not mean the import finished.
KB_MIGRATION = "knowledge_entries_v1"
_SUMMARY_MAX_AGE = 86400  # 24 hours
_SUMMARY_NEW_ENTRY_THRESHOLD = 20


def _storage() -> "OuroborosStorage":
    from .storage import OuroborosStorage

    return OuroborosStorage()


def entry_fingerprint(entry: Dict[str, Any]) -> str:
    """Stable identity for an entry, which carries no id of its own."""
    from .storage import OuroborosStorage

    return OuroborosStorage.record_fingerprint(
        json.dumps(entry, sort_keys=True, default=str)
    )


def _load_scalars(path: Optional[str] = None) -> Dict[str, Any]:
    """Read the summary cache, which stays in JSON.

    Two small fields that never grow, so there is nothing to gain from moving
    them; only the unbounded entries list needed a table.
    """
    p = path or KB_PATH
    return load_json_file(
        p,
        default=KB_DEFAULT,
        error_msg=f"Corrupt knowledge base at {p}, starting fresh",
        logger=log,
    )


def load_kb(path: Optional[str] = None) -> Dict[str, Any]:
    """Return the knowledge base: entries from SQLite, cache from JSON.

    Any entries still in the JSON file are imported on first read, so an
    existing deployment needs no manual step.
    """
    kb = dict(_load_scalars(path))
    storage = _storage()

    legacy = kb.get("entries") or []
    if storage.migration_done(KB_MIGRATION):
        legacy = []
    if legacy:
        # save_kb stops writing entries, so the next summary refresh would
        # erase them from the file. Keep a copy before that happens.
        from .state_migration import freeze_rollback_snapshot

        if freeze_rollback_snapshot(Path(path or KB_PATH)) is None:
            log.warning("Skipping knowledge cutover: could not snapshot the file")
            return kb
        imported = sum(
            1 for entry in legacy
            if isinstance(entry, dict)
            and storage.append_knowledge(entry, entry_fingerprint(entry))
        )
        storage.mark_migration_done(KB_MIGRATION)
        if imported:
            log.info("Imported %d knowledge entries from JSON into SQLite", imported)

    kb["entries"] = storage.get_knowledge_entries()
    return kb


def save_kb(kb: Dict[str, Any], path: Optional[str] = None) -> None:
    """Persist the summary cache. Entries are owned by SQLite."""
    p = path or KB_PATH
    scalars = {k: v for k, v in kb.items() if k != "entries"}
    save_json_file(p, scalars, sort_keys=True)


def add_entries(entries: List[Dict[str, Any]], path: Optional[str] = None) -> None:
    """Append entries. No cap -- appends no longer rewrite the whole list."""
    if not entries:
        return
    pairs = [
        (entry, entry_fingerprint(entry))
        for entry in entries
        if isinstance(entry, dict)
    ]
    _storage().append_knowledge_batch(pairs)


def get_summary(
    client: Any,
    kb: Optional[Dict[str, Any]] = None,
    force_refresh: bool = False,
    path: Optional[str] = None,
) -> str:
    """Return a cached summary of KB entries, regenerating if stale.

    Regenerates if >24h old or >20 new entries since last summary.
    """
    if kb is None:
        kb = load_kb(path)

    entries = kb.get("entries", [])
    if not entries:
        return ""

    cached = kb.get("summary_cache", "")
    # The default only covers an absent key -- an explicit null, which is what
    # an interrupted or hand-edited write leaves behind, still lands here.
    updated_at = kb.get("summary_updated_at") or 0
    now = int(time.time())

    # Count entries added since last summary
    entries_since = sum(1 for e in entries if e.get("ts", 0) > updated_at)

    needs_refresh = (
        force_refresh
        or not cached
        or (now - updated_at) > _SUMMARY_MAX_AGE
        or entries_since >= _SUMMARY_NEW_ENTRY_THRESHOLD
    )

    if not needs_refresh:
        return cached

    # Generate new summary
    from . import llm as _llm

    summary = _llm.generate_kb_summary(client, entries)
    if summary:
        kb["summary_cache"] = summary
        kb["summary_updated_at"] = now
        save_kb(kb, path)
        return summary

    # Fallback to cached if generation fails
    return cached
