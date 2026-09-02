"""One-way backfill of the JSON history files into SQLite.

The three append-only collections named in the SQLite migration --
comment_history, improvement_history and the knowledge base -- used to live in
JSON files that were read and rewritten whole on every cycle. Each therefore
carried a cap, and the agent continuously discarded its own history to keep the
files small.

This module moves what exists into the database once. It is deliberately
conservative:

* Idempotent. Every insert is INSERT OR IGNORE against a natural key, so
  running it twice adds nothing. Safe to call on every start.
* Non-destructive. Nothing is deleted. The JSON files are left exactly where
  they are, so rolling back is a matter of deploying the previous version.
* Additive. If a record cannot be read it is counted and skipped rather than
  aborting the run, since a single bad row should not keep the agent from
  starting.
"""

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .storage import OuroborosStorage, load_json_file

log = logging.getLogger(__name__)

# Marker recorded in the DB so a completed backfill is not re-attempted, and so
# `status` can say whether it has run.
_MIGRATION_KEY = "json_history_v1"


@dataclass
class MigrationReport:
    comments: int = 0
    improvements: int = 0
    knowledge: int = 0
    skipped: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.comments + self.improvements + self.knowledge

    def summary(self) -> str:
        parts = [
            f"{self.comments} comments",
            f"{self.improvements} improvements",
            f"{self.knowledge} knowledge entries",
        ]
        text = "migrated " + ", ".join(parts)
        if self.skipped:
            text += f" ({len(self.skipped)} records skipped)"
        return text


def _entry_fingerprint(entry: Dict[str, Any]) -> str:
    """Delegate to the knowledge_base implementation.

    load_kb imports legacy entries too. Two hashes of the same entry means
    whichever path runs second inserts it again.
    """
    from .knowledge_base import entry_fingerprint

    return entry_fingerprint(entry)


def freeze_rollback_snapshot(path: Path) -> Optional[Path]:
    """Copy a legacy file aside once, before it stops being authoritative.

    Both state.json and knowledge_base.json keep being written after the
    cutover -- for the runtime pointers and the summary cache -- but without
    the collections that moved. The first such write would erase the very
    records the rollback story depends on, so the pre-cutover contents are
    preserved next to the original.
    """
    if not path.exists():
        return None
    snapshot = path.with_name(path.name + ".pre-sqlite")
    if snapshot.exists() and snapshot.stat().st_size > 0:
        return snapshot

    # Temp file then atomic rename: writing straight to the destination can
    # leave a truncated file that the next call accepts merely because it
    # exists, which is worse than no snapshot at all.
    tmp = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
        os.close(fd)
        tmp = Path(tmp_name)
        shutil.copy2(path, tmp)
        if tmp.stat().st_size != path.stat().st_size:
            raise OSError("snapshot size mismatch")
        os.replace(tmp, snapshot)
        log.info("Kept a pre-migration copy of %s at %s", path.name, snapshot.name)
        return snapshot
    except OSError:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        log.warning("Could not snapshot %s for rollback", path, exc_info=True)
        return None


def migrate_json_history(
    repo_root: Path,
    storage: Optional[OuroborosStorage] = None,
    *,
    state_path: Optional[Path] = None,
    history_path: Optional[Path] = None,
    kb_path: Optional[Path] = None,
) -> MigrationReport:
    """Backfill the JSON history files into SQLite. Idempotent."""
    store = storage or OuroborosStorage()
    report = MigrationReport()

    # -- comment_history, which lives inside state.json ---------------------
    state_file = state_path or Path(
        os.path.expanduser("~/.config/moltbook/state.json")
    )
    freeze_rollback_snapshot(state_file)
    state = load_json_file(state_file, default={})
    comments = state.get("comment_history") if isinstance(state, dict) else None
    for record in comments if isinstance(comments, list) else []:
        if not isinstance(record, dict):
            report.skipped.append("comment_history: non-object record")
            continue
        try:
            if store.append_comment(record):
                report.comments += 1
        except Exception as exc:  # pragma: no cover - defensive
            report.skipped.append(f"comment_history: {exc}")

    # -- improvement_history, its own file in the repo ----------------------
    hist_file = history_path or (repo_root / "config" / "improvement_history.json")
    history = load_json_file(hist_file, default=[])
    for record in history if isinstance(history, list) else []:
        if not isinstance(record, dict):
            report.skipped.append("improvement_history: non-object record")
            continue
        try:
            if store.append_improvement(record):
                report.improvements += 1
        except Exception as exc:  # pragma: no cover - defensive
            report.skipped.append(f"improvement_history: {exc}")

    # -- knowledge base, outside the repo -----------------------------------
    from .knowledge_base import KB_PATH

    kb_file = kb_path or Path(KB_PATH)
    freeze_rollback_snapshot(kb_file)
    kb = load_json_file(kb_file, default={})
    entries = kb.get("entries") if isinstance(kb, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            report.skipped.append("knowledge_base: non-object entry")
            continue
        try:
            if store.append_knowledge(entry, _entry_fingerprint(entry)):
                report.knowledge += 1
        except Exception as exc:  # pragma: no cover - defensive
            report.skipped.append(f"knowledge_base: {exc}")

    if report.total or report.skipped:
        log.info("State migration: %s", report.summary())
    for reason in report.skipped:
        log.warning("State migration skipped a record -- %s", reason)

    return report
