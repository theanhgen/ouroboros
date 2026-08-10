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
            # Append-only history. These lived in JSON files that were read
            # and rewritten whole on every cycle, which is why each had a cap
            # -- comment_history alone was 102 KB of a 169 KB state.json. The
            # caps meant the agent continuously discarded its own history to
            # keep the file small; a table has no such pressure.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS comment_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    post_id TEXT,
                    comment_id TEXT,
                    -- Content hash, not (post_id, comment_id): comment_id is
                    -- null on every historical record, and SQLite treats NULL
                    -- as distinct from NULL, so a UNIQUE over those columns
                    -- would let a re-run insert everything again.
                    fingerprint TEXT UNIQUE,
                    payload TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_comment_history_ts "
                "ON comment_history (ts DESC)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS improvement_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    task_id TEXT,
                    outcome TEXT,
                    -- Identity is the task and its timestamp, but hashed for
                    -- the same NULL reason as above.
                    fingerprint TEXT UNIQUE,
                    payload TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_improvement_history_ts "
                "ON improvement_history (ts DESC)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    fingerprint TEXT UNIQUE,
                    payload TEXT NOT NULL
                )
            """)
            # Explicit completion markers. Inferring "already migrated" from
            # a non-empty table is wrong: a partial import leaves rows behind,
            # and the next start would skip the remaining legacy records and
            # then strip the file that still held them.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS migrations (
                    name TEXT PRIMARY KEY,
                    completed_at REAL NOT NULL
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

    # -- Append-only history ------------------------------------------------
    #
    # Each row keeps the whole record as JSON alongside the few columns worth
    # indexing. The shapes here are produced by an LLM and have changed before,
    # so pinning every field into a column would mean a schema migration each
    # time one gains a key.

    @staticmethod
    def record_fingerprint(*parts: Any) -> str:
        """Stable identity for a record with no reliable natural key.

        A UNIQUE over nullable columns does not dedupe, because SQLite treats
        every NULL as distinct. Hashing the identifying values sidesteps that.
        """
        import hashlib

        # json rather than str-join: it encodes None distinctly from "", so a
        # missing field and an empty one cannot hash to the same record.
        joined = json.dumps([None if p is None else str(p) for p in parts])
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _timestamp(value: Any) -> float:
        """Coerce a record timestamp, treating only None/invalid as missing.

        `value or time.time()` would replace a legitimate 0.0 with now, which
        reorders the oldest record to the newest.
        """
        try:
            return float(value)
        except (TypeError, ValueError):
            return time.time()

    def append_comment(self, record: Dict[str, Any]) -> bool:
        """Record a posted comment. Returns False if it was already recorded."""
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO comment_history "
                "(ts, post_id, comment_id, fingerprint, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    self._timestamp(record.get("ts")),
                    record.get("post_id"),
                    record.get("comment_id"),
                    self.record_fingerprint(
                        record.get("post_id"),
                        record.get("comment_id"),
                        record.get("comment"),
                    ),
                    json.dumps(record),
                ),
            )
            return cursor.rowcount > 0

    def get_comment_history(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return comments oldest-first, or the newest `limit` of them."""
        return self._recent_payloads("comment_history", limit)

    def comment_count(self) -> int:
        return self._count("comment_history")

    def append_improvement(self, record: Dict[str, Any]) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO improvement_history "
                "(ts, task_id, outcome, fingerprint, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    self._timestamp(record.get("timestamp")),
                    record.get("task_id"),
                    record.get("outcome"),
                    self.record_fingerprint(
                        record.get("task_id"), record.get("timestamp")
                    ),
                    json.dumps(record),
                ),
            )
            return cursor.rowcount > 0

    def update_improvement(self, task_id: str, ts: float, record: Dict[str, Any]) -> bool:
        """Rewrite one record in place, for PR outcome polling."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE improvement_history SET outcome = ?, payload = ? "
                "WHERE fingerprint = ?",
                (
                    record.get("outcome"),
                    json.dumps(record),
                    self.record_fingerprint(task_id, ts),
                ),
            )
            return cursor.rowcount > 0

    def get_improvement_history(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        return self._recent_payloads("improvement_history", limit)

    def improvement_count(self) -> int:
        return self._count("improvement_history")

    def append_knowledge_batch(self, entries: List[tuple]) -> int:
        """Insert (entry, fingerprint) pairs in one transaction.

        Per-entry commits leave earlier rows written and later ones lost if
        one fails partway, and the caller has already marked the source posts
        seen -- so the missing insights are never regenerated.
        """
        if not entries:
            return 0
        with self._connect() as conn:
            cursor = conn.executemany(
                "INSERT OR IGNORE INTO knowledge_entries (ts, fingerprint, payload) "
                "VALUES (?, ?, ?)",
                [
                    (self._timestamp(entry.get("ts")), fingerprint, json.dumps(entry))
                    for entry, fingerprint in entries
                ],
            )
            return cursor.rowcount

    def append_knowledge(self, entry: Dict[str, Any], fingerprint: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO knowledge_entries (ts, fingerprint, payload) "
                "VALUES (?, ?, ?)",
                (self._timestamp(entry.get("ts")), fingerprint, json.dumps(entry)),
            )
            return cursor.rowcount > 0

    def get_knowledge_entries(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        return self._recent_payloads("knowledge_entries", limit)

    def knowledge_count(self) -> int:
        return self._count("knowledge_entries")

    def _recent_payloads(self, table: str, limit: Optional[int]) -> List[Dict[str, Any]]:
        """Newest `limit` rows, returned oldest-first.

        Ordered by insertion (id), not timestamp. The JSON lists these replace
        were in append order, and callers slice them with [-n:] to mean "most
        recent". Sorting by ts would reorder history whenever the Pi's clock
        was corrected, or whenever a record carried a missing or bogus
        timestamp, and change which records a tail slice selects.
        """
        with self._connect() as conn:
            if limit is None:
                rows = conn.execute(
                    f"SELECT payload FROM {table} ORDER BY id ASC"  # noqa: S608
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT payload FROM ("  # noqa: S608
                    f"  SELECT payload, id FROM {table} ORDER BY id DESC LIMIT ?"
                    f") ORDER BY id ASC",
                    (limit,),
                ).fetchall()
        out = []
        for (payload,) in rows:
            try:
                out.append(json.loads(payload))
            except (json.JSONDecodeError, TypeError):
                log.warning("Skipping unreadable %s row", table)
        return out

    def migration_done(self, name: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM migrations WHERE name = ?", (name,)
            ).fetchone()
        return row is not None

    def mark_migration_done(self, name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO migrations (name, completed_at) VALUES (?, ?)",
                (name, time.time()),
            )

    def mark_migration_pending(self, name: str) -> None:
        """Clear a completion marker so the import runs again."""
        with self._connect() as conn:
            conn.execute("DELETE FROM migrations WHERE name = ?", (name,))

    def _count(self, table: str) -> int:
        with self._connect() as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
