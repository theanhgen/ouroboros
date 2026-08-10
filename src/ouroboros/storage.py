"""Local storage helpers for tracking autonomous cycles and metrics."""

import copy
import fcntl
import json
import logging
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from dataclasses import dataclass

from .codebase import get_repo_root

log = logging.getLogger(__name__)
_MISSING = object()


def load_json_file(
    path: Union[str, Path],
    default: Any = _MISSING,
    *,
    error_msg: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Any:
    """Load a JSON file, returning a default for missing or corrupt files.

    A read error other than "not there" propagates. Permission denied or a
    failing disk means the data may well exist and simply could not be read;
    handing back the default would let a caller that loads-modifies-writes
    overwrite the real contents with an empty one.

    The file is opened directly rather than probed with exists() first.
    Path.exists() answers False for any stat error, so a preflight check
    would quietly turn EACCES or EIO back into "missing" and reintroduce
    exactly that overwrite.
    """
    json_path = Path(path).expanduser()

    try:
        handle = json_path.open("r", encoding="utf-8")
    except FileNotFoundError:
        if default is _MISSING:
            raise
        return copy.deepcopy(default)

    try:
        with handle as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
        # UnicodeDecodeError, not just JSONDecodeError: a file zeroed or filled
        # with binary garbage by a crash never reaches the JSON parser, and
        # that is precisely the corruption the default exists for.
        if default is _MISSING:
            raise
        if error_msg:
            (logger or log).warning(error_msg)
        return copy.deepcopy(default)


def save_json_file(
    path: Union[str, Path],
    data: Any,
    *,
    sort_keys: bool = False,
    indent: int = 2,
) -> None:
    """Safely write JSON via a temp file, fsync, and atomic replace.

    The temp file name is unique per writer. A fixed "<name>.tmp" is shared
    state: two processes writing the same target would open and truncate the
    same scratch file, and whichever renamed second could publish a file the
    other was still writing.

    The lock is taken on the containing directory rather than on a lock file.
    Several of these targets live inside the repository, and any extra file
    left beside them shows up as untracked -- commit_auto_state would not
    stage it and git_ops.is_clean counts untracked as dirty, so every
    subsequent improvement cycle would abort with a dirty worktree. Locking
    the directory needs no file at all. It serialises every write in that
    directory rather than just this one, which for a handful of small state
    files is not worth a more precise scheme.

    A failure anywhere before the rename removes the temp file, so a full disk
    or a serialisation error does not leave scratch files accumulating.
    """
    json_path = Path(path).expanduser()
    json_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(json_path.parent), prefix=f".{json_path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)

    try:
        dir_fd = os.open(str(json_path.parent), os.O_RDONLY)
        try:
            fcntl.flock(dir_fd, fcntl.LOCK_EX)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=indent, sort_keys=sort_keys)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, json_path)
            finally:
                fcntl.flock(dir_fd, fcntl.LOCK_UN)
        finally:
            os.close(dir_fd)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


@dataclass
class CycleRecord:
    id: Optional[int] = None
    ts: float = 0.0
    task_type: str = ""
    model: str = ""
    status: str = ""
    description: str = ""


@dataclass
class MetricRecord:
    cycle_id: int
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0


class OuroborosStorage:
    def _connect(self) -> sqlite3.Connection:
        """Open a connection with the declared foreign key actually enforced.

        SQLite defaults foreign_keys to OFF per connection, so the REFERENCES
        clause on metrics was decorative: a MetricRecord for a cycle that does
        not exist was accepted, counted by get_total_cost, and invisible to
        get_recent_cycles, which reports a cost no cycle accounts for.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = get_repo_root() / "config" / "ouroboros.db"
        
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    task_type TEXT,
                    model TEXT,
                    status TEXT,
                    description TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    cycle_id INTEGER PRIMARY KEY,
                    tokens_in INTEGER DEFAULT 0,
                    tokens_out INTEGER DEFAULT 0,
                    cost REAL DEFAULT 0.0,
                    FOREIGN KEY (cycle_id) REFERENCES cycles (id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_type TEXT, -- 'code' | 'failure' | 'cycle'
                    ref_id TEXT, -- file path, task_id, etc.
                    content TEXT,
                    embedding TEXT, -- JSON string of float list
                    ts REAL
                )
            """)

    def record_cycle(self, record: CycleRecord) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO cycles (ts, task_type, model, status, description) VALUES (?, ?, ?, ?, ?)",
                (record.ts or time.time(), record.task_type, record.model, record.status, record.description)
            )
            return cursor.lastrowid

    def record_metrics(self, metrics: MetricRecord):
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO metrics (cycle_id, tokens_in, tokens_out, cost) VALUES (?, ?, ?, ?)",
                (metrics.cycle_id, metrics.tokens_in, metrics.tokens_out, metrics.cost)
            )

    def get_recent_cycles(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT c.*, m.tokens_in, m.tokens_out, m.cost FROM cycles c "
                "LEFT JOIN metrics m ON c.id = m.cycle_id "
                "ORDER BY c.ts DESC LIMIT ?",
                (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_total_cost(self) -> float:
        with self._connect() as conn:
            cursor = conn.execute("SELECT SUM(cost) FROM metrics")
            row = cursor.fetchone()
            return row[0] if row[0] is not None else 0.0

    def add_embedding(self, content_type: str, ref_id: str, content: str, embedding: List[float]):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO embeddings (content_type, ref_id, content, embedding, ts) VALUES (?, ?, ?, ?, ?)",
                (content_type, ref_id, content, json.dumps(embedding), time.time())
            )

    def search_embeddings(self, content_type: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Return raw embeddings for local similarity search."""
        query = "SELECT * FROM embeddings"
        params = []
        if content_type:
            query += " WHERE content_type = ?"
            params.append(content_type)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
