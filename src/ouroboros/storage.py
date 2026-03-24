"""Local SQLite storage for tracking autonomous cycles and metrics."""

import sqlite3
import time
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

from .codebase import get_repo_root


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
    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = get_repo_root() / "config" / "ouroboros.db"
        
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO cycles (ts, task_type, model, status, description) VALUES (?, ?, ?, ?, ?)",
                (record.ts or time.time(), record.task_type, record.model, record.status, record.description)
            )
            return cursor.lastrowid

    def record_metrics(self, metrics: MetricRecord):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO metrics (cycle_id, tokens_in, tokens_out, cost) VALUES (?, ?, ?, ?)",
                (metrics.cycle_id, metrics.tokens_in, metrics.tokens_out, metrics.cost)
            )

    def get_recent_cycles(self, limit: int = 10) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT c.*, m.tokens_in, m.tokens_out, m.cost FROM cycles c "
                "LEFT JOIN metrics m ON c.id = m.cycle_id "
                "ORDER BY c.ts DESC LIMIT ?",
                (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_total_cost(self) -> float:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT SUM(cost) FROM metrics")
            row = cursor.fetchone()
            return row[0] if row[0] is not None else 0.0

    def add_embedding(self, content_type: str, ref_id: str, content: str, embedding: List[float]):
        with sqlite3.connect(self.db_path) as conn:
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
        
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
