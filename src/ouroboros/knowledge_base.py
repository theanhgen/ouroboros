"""Knowledge base -- persisted insights from community posts."""

import logging
import os
import time
from typing import Any, Dict, List, Optional

from .storage import load_json_file, save_json_file

log = logging.getLogger(__name__)

KB_PATH = os.path.expanduser("~/.config/moltbook/knowledge_base.json")
KB_DEFAULT = {"entries": [], "summary_cache": "", "summary_updated_at": 0}
MAX_ENTRIES = 200
_SUMMARY_MAX_AGE = 86400  # 24 hours
_SUMMARY_NEW_ENTRY_THRESHOLD = 20


def load_kb(path: Optional[str] = None) -> Dict[str, Any]:
    """Load the knowledge base from disk."""
    p = path or KB_PATH
    return load_json_file(
        p,
        default=KB_DEFAULT,
        error_msg=f"Corrupt knowledge base at {p}, starting fresh",
        logger=log,
    )


def save_kb(kb: Dict[str, Any], path: Optional[str] = None) -> None:
    """Atomic write of the knowledge base."""
    p = path or KB_PATH
    save_json_file(p, kb, sort_keys=True)


def add_entries(entries: List[Dict[str, Any]], path: Optional[str] = None) -> None:
    """Append entries to the knowledge base and trim to MAX_ENTRIES."""
    if not entries:
        return
    kb = load_kb(path)
    kb["entries"].extend(entries)
    if len(kb["entries"]) > MAX_ENTRIES:
        kb["entries"] = kb["entries"][-MAX_ENTRIES:]
    save_kb(kb, path)


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
    updated_at = kb.get("summary_updated_at", 0)
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
