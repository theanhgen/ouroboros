"""Long-term memory with holographic reduced representations.

Replaces the old OpenAI-embedding IndexManager with a fully local system:
- SQLite-backed fact store with FTS5 full-text search
- HRR phase vectors for compositional retrieval (no API calls)
- Entity extraction, trust scoring, and memory bank management
- Hybrid retrieval: FTS5 + Jaccard + HRR similarity

Inspired by NousResearch/hermes-agent's holographic memory plugin.
"""

import ast
import copy
import logging
import math
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import holographic as hrr
from .codebase import get_repo_root

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL UNIQUE,
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_facts_trust    ON facts(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_entities_name  ON entities(name);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(content, tags, content=facts, content_rowid=fact_id);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TABLE IF NOT EXISTS memory_banks (
    bank_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_name  TEXT NOT NULL UNIQUE,
    vector     BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    fact_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# Trust constants
_HELPFUL_DELTA = 0.05
_UNHELPFUL_DELTA = -0.10
_TRUST_MIN = 0.0
_TRUST_MAX = 1.0

# Entity extraction patterns
_RE_CAPITALIZED = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b')
_RE_DOUBLE_QUOTE = re.compile(r'"([^"]+)"')
_RE_SINGLE_QUOTE = re.compile(r"'([^']+)'")


def _clamp_trust(value: float) -> float:
    return max(_TRUST_MIN, min(_TRUST_MAX, value))


def _fts5_match_query(text: str) -> str:
    """Compile arbitrary text into a literal FTS5 MATCH expression.

    FTS5 reads its own operators out of the query string, so ordinary text
    containing a colon, an unbalanced quote, a trailing ``AND`` or a bare
    ``*`` is a syntax error rather than a search term. Quoting each
    whitespace-separated token turns it into a phrase, which FTS5 matches
    literally: ``issue:123`` searches for "issue:123" instead of failing on
    an unknown column. Embedded double quotes are escaped by doubling.

    Returns "" when the text has no searchable tokens, which callers treat
    as "no results" without touching the database.

    NUL is replaced rather than quoted: SQLite stops reading the MATCH
    expression at U+0000, so a quoted phrase containing one is reported as
    an unterminated string no matter how it is escaped. It is the only
    control character that survives quoting.
    """
    tokens = [token for token in text.replace("\x00", " ").split() if token.strip()]
    return " ".join('"' + token.replace('"', '""') + '"' for token in tokens)


# ---------------------------------------------------------------------------
# Code indexing
# ---------------------------------------------------------------------------

class CodeASTVisitor(ast.NodeVisitor):
    """AST visitor to extract classes, methods, functions and docstrings from Python files."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.facts: List[str] = []
        self.class_stack: List[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        class_name = node.name
        if self.class_stack:
            class_name = ".".join(self.class_stack) + "." + class_name

        self.facts.append(f"[code] {self.file_path}: class {class_name}")
        docstring = ast.get_docstring(node)
        if docstring:
            self.facts.append(f"[code] {self.file_path}: class {class_name} docstring: {docstring.strip()}")

        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func(node)

    def _visit_func(self, node: Any) -> None:
        func_name = node.name
        if self.class_stack:
            func_name = ".".join(self.class_stack) + "." + func_name

        dummy = copy.copy(node)
        dummy.body = [ast.Pass()]
        dummy.decorator_list = []
        dummy.name = func_name

        try:
            sig = ast.unparse(dummy).strip().removesuffix(":\n    pass")
            self.facts.append(f"[code] {self.file_path}: {sig}")
        except Exception:
            self.facts.append(f"[code] {self.file_path}: def {func_name}")

        docstring = ast.get_docstring(node)
        if docstring:
            self.facts.append(f"[code] {self.file_path}: def {func_name} docstring: {docstring.strip()}")


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

class MemoryStore:
    """SQLite-backed fact store with entity resolution, trust scoring, and HRR vectors."""

    def __init__(self, db_path: Optional[Path] = None, hrr_dim: int = 1024) -> None:
        # Checked here, before the database is touched: add_fact commits the
        # fact row before computing its HRR vector, so letting a bad hrr_dim
        # reach encode_atom would leave a committed row with a NULL vector
        # that the dedup path then reports as a success on retry.
        if hrr_dim <= 0:
            raise ValueError(f"hrr_dim must be positive, got {hrr_dim}")
        if db_path is None:
            db_path = get_repo_root() / "config" / "memory.db"
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.hrr_dim = hrr_dim
        self._hrr_available = hrr.HAS_NUMPY
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10.0)
        self._lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- Public API ----------------------------------------------------------

    def add_fact(self, content: str, category: str = "general", tags: str = "") -> int:
        """Insert a fact. Returns fact_id. Deduplicates by content."""
        with self._lock:
            content = content.strip()
            if not content:
                raise ValueError("content must not be empty")
            try: 
                cur = self._conn.execute(
                    "INSERT INTO facts (content, category, tags, trust_score) VALUES (?, ?, ?, ?)",
                    (content, category, tags, 0.5),
                )
                self._conn.commit()
                fact_id: int = cur.lastrowid  # type: ignore[assignment]
            except sqlite3.IntegrityError:
                row = self._conn.execute(
                    "SELECT fact_id FROM facts WHERE content = ?", (content,)
                ).fetchone()
                return int(row["fact_id"])

            for name in self._extract_entities(content):
                entity_id = self._resolve_entity(name)
                self._link_fact_entity(fact_id, entity_id)

            self._compute_hrr_vector(fact_id, content)
            self._rebuild_bank(category)
            return fact_id

    def index_code(self, file_path: str, content: str) -> List[int]:
        """Parse code into code facts and persist them.

        Python files are parsed with AST to extract module docstrings, classes,
        methods, standalone functions, and docstrings. Non-Python files, parse
        failures, and Python files with no extractable facts fall back to a
        content prefix. Returns the created or existing fact IDs.
        """
        facts: List[str] = []

        if file_path.lower().endswith(".py"):
            try:
                tree = ast.parse(content)
                module_doc = ast.get_docstring(tree)
                if module_doc:
                    facts.append(f"[code] {file_path}: module docstring: {module_doc.strip()}")

                visitor = CodeASTVisitor(file_path)
                visitor.visit(tree)
                facts.extend(visitor.facts)
            except Exception as e:
                log.warning("Failed to parse Python file %s with AST: %s", file_path, e)

        if not facts:
            facts.append(f"[code] {file_path}: {content[:500]}")

        fact_ids: List[int] = []
        for fact_content in facts:
            fact_ids.append(self.add_fact(fact_content, category="code", tags=file_path))
        return fact_ids

    def search_facts(self, query: str, category: Optional[str] = None,
                     min_trust: float = 0.3, limit: int = 10) -> List[Dict]:
        """Full-text search over facts using FTS5.

        ``query`` is treated as literal text, not as an FTS5 expression, so
        arbitrary input is safe to pass through.
        """
        with self._lock:
            match_query = _fts5_match_query(query)
            if not match_query:
                return []
            params: list = [match_query, min_trust]
            cat_clause = ""
            if category is not None:
                cat_clause = "AND f.category = ?"
                params.append(category)
            params.append(limit)
            sql = f"""
                SELECT f.fact_id, f.content, f.category, f.tags,
                       f.trust_score, f.retrieval_count, f.helpful_count,
                       f.created_at, f.updated_at
                FROM facts f
                JOIN facts_fts fts ON fts.rowid = f.fact_id
                WHERE facts_fts MATCH ?
                  AND f.trust_score >= ?
                  {cat_clause}
                ORDER BY fts.rank, f.trust_score DESC
                LIMIT ?
            """
            rows = self._conn.execute(sql, params).fetchall()
            results = [dict(r) for r in rows]
            if results:
                ids = [r["fact_id"] for r in results]
                ph = ",".join("?" * len(ids))
                self._conn.execute(
                    f"UPDATE facts SET retrieval_count = retrieval_count + 1 WHERE fact_id IN ({ph})",
                    ids,
                )
                self._conn.commit()
            return results

    def update_fact(self, fact_id: int, content: Optional[str] = None,
                    trust_delta: Optional[float] = None, tags: Optional[str] = None,
                    category: Optional[str] = None) -> bool:
        """Partially update a fact. Returns True if row existed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id, trust_score, category FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                return False

            assignments = ["updated_at = CURRENT_TIMESTAMP"]
            params: list = []
            if content is not None:
                assignments.append("content = ?")
                params.append(content.strip())
            if tags is not None:
                assignments.append("tags = ?")
                params.append(tags)
            if category is not None:
                assignments.append("category = ?")
                params.append(category)
            if trust_delta is not None:
                new_trust = _clamp_trust(row["trust_score"] + trust_delta)
                assignments.append("trust_score = ?")
                params.append(new_trust)

            params.append(fact_id)
            self._conn.execute(
                f"UPDATE facts SET {', '.join(assignments)} WHERE fact_id = ?", params,
            )
            self._conn.commit()

            if content is not None:
                self._conn.execute("DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,))
                for name in self._extract_entities(content):
                    eid = self._resolve_entity(name)
                    self._link_fact_entity(fact_id, eid)
                self._conn.commit()
                self._compute_hrr_vector(fact_id, content)

            cat = category or row["category"]
            self._rebuild_bank(cat)
            return True

    def remove_fact(self, fact_id: int) -> bool:
        """Delete a fact and its entity links."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id, category FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute("DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,))
            self._conn.execute("DELETE FROM facts WHERE fact_id = ?", (fact_id,))
            self._conn.commit()
            self._rebuild_bank(row["category"])
            return True

    def list_facts(self, category: Optional[str] = None, min_trust: float = 0.0,
                   limit: int = 50) -> List[Dict]:
        """Browse facts ordered by trust_score descending."""
        with self._lock:
            params: list = [min_trust]
            cat_clause = ""
            if category is not None:
                cat_clause = "AND category = ?"
                params.append(category)
            params.append(limit)
            sql = f"""
                SELECT fact_id, content, category, tags, trust_score,
                       retrieval_count, helpful_count, created_at, updated_at
                FROM facts WHERE trust_score >= ? {cat_clause}
                ORDER BY trust_score DESC LIMIT ?
            """
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def record_feedback(self, fact_id: int, helpful: bool) -> Dict:
        """Adjust trust based on feedback. Returns old/new trust."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id, trust_score, helpful_count FROM facts WHERE fact_id = ?",
                (fact_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"fact_id {fact_id} not found")
            old_trust = row["trust_score"]
            delta = _HELPFUL_DELTA if helpful else _UNHELPFUL_DELTA
            new_trust = _clamp_trust(old_trust + delta)
            inc = 1 if helpful else 0
            self._conn.execute(
                "UPDATE facts SET trust_score = ?, helpful_count = helpful_count + ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE fact_id = ?",
                (new_trust, inc, fact_id),
            )
            self._conn.commit()
            return {"fact_id": fact_id, "old_trust": old_trust, "new_trust": new_trust}

    def fact_count(self) -> int:
        """Total number of facts stored."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()
            return row[0] if row else 0

    def close(self) -> None:
        self._conn.close()

    # -- Entity helpers ------------------------------------------------------

    def _extract_entities(self, text: str) -> List[str]:
        seen: set = set()
        candidates: list = []

        def _add(name: str) -> None:
            s = name.strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                candidates.append(s)

        for m in _RE_CAPITALIZED.finditer(text):
            _add(m.group(1))
        for m in _RE_DOUBLE_QUOTE.finditer(text):
            _add(m.group(1))
        for m in _RE_SINGLE_QUOTE.finditer(text):
            _add(m.group(1))
        return candidates

    def _resolve_entity(self, name: str) -> int:
        row = self._conn.execute(
            "SELECT entity_id FROM entities WHERE name LIKE ?", (name,)
        ).fetchone()
        if row is not None:
            return int(row["entity_id"])
        alias_row = self._conn.execute(
            "SELECT entity_id FROM entities WHERE ',' || aliases || ',' LIKE '%,' || ? || ',%'",
            (name,),
        ).fetchone()
        if alias_row is not None:
            return int(alias_row["entity_id"])
        cur = self._conn.execute("INSERT INTO entities (name) VALUES (?)", (name,))
        self._conn.commit()
        return int(cur.lastrowid)  # type: ignore[return-value]

    def _link_fact_entity(self, fact_id: int, entity_id: int) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?, ?)",
            (fact_id, entity_id),
        )
        self._conn.commit()

    def _compute_hrr_vector(self, fact_id: int, content: str) -> None:
        if not self._hrr_available:
            return
        rows = self._conn.execute(
            "SELECT e.name FROM entities e "
            "JOIN fact_entities fe ON fe.entity_id = e.entity_id "
            "WHERE fe.fact_id = ?", (fact_id,),
        ).fetchall()
        entities = [row["name"] for row in rows]
        vector = hrr.encode_fact(content, entities, self.hrr_dim)
        self._conn.execute(
            "UPDATE facts SET hrr_vector = ? WHERE fact_id = ?",
            (hrr.phases_to_bytes(vector), fact_id),
        )
        self._conn.commit()

    def _rebuild_bank(self, category: str) -> None:
        if not self._hrr_available:
            return
        bank_name = f"cat:{category}"
        rows = self._conn.execute(
            "SELECT hrr_vector FROM facts WHERE category = ? AND hrr_vector IS NOT NULL",
            (category,),
        ).fetchall()
        if not rows:
            self._conn.execute("DELETE FROM memory_banks WHERE bank_name = ?", (bank_name,))
            self._conn.commit()
            return
        vectors = [hrr.bytes_to_phases(row["hrr_vector"]) for row in rows]
        bank_vector = hrr.bundle(*vectors)
        hrr.snr_estimate(self.hrr_dim, len(vectors))
        self._conn.execute(
            "INSERT INTO memory_banks (bank_name, vector, dim, fact_count, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(bank_name) DO UPDATE SET "
            "vector=excluded.vector, dim=excluded.dim, fact_count=excluded.fact_count, "
            "updated_at=excluded.updated_at",
            (bank_name, hrr.phases_to_bytes(bank_vector), self.hrr_dim, len(vectors)),
        )
        self._conn.commit()


# ---------------------------------------------------------------------------
# FactRetriever -- hybrid keyword/HRR retrieval
# ---------------------------------------------------------------------------

class FactRetriever:
    """Multi-strategy fact retrieval with trust-weighted scoring."""

    def __init__(self, store: MemoryStore, fts_weight: float = 0.4,
                 jaccard_weight: float = 0.3, hrr_weight: float = 0.3) -> None:
        self.store = store
        self.hrr_dim = store.hrr_dim
        if hrr_weight > 0 and not hrr.HAS_NUMPY:
            fts_weight, jaccard_weight, hrr_weight = 0.6, 0.4, 0.0
        self.fts_weight = fts_weight
        self.jaccard_weight = jaccard_weight
        self.hrr_weight = hrr_weight

    def search(self, query: str, category: Optional[str] = None,
               min_trust: float = 0.3, limit: int = 10) -> List[Dict]:
        """Hybrid search: FTS5 candidates -> Jaccard + HRR rerank -> trust weighting."""
        candidates = self._fts_candidates(query, category, min_trust, limit * 3)
        if not candidates:
            return []

        query_tokens = self._tokenize(query)
        scored = []
        for fact in candidates:
            content_tokens = self._tokenize(fact["content"])
            tag_tokens = self._tokenize(fact.get("tags", ""))
            jaccard = self._jaccard(query_tokens, content_tokens | tag_tokens)
            fts_score = fact.get("fts_rank", 0.0)

            if self.hrr_weight > 0 and fact.get("hrr_vector"):
                fact_vec = hrr.bytes_to_phases(fact["hrr_vector"])
                query_vec = hrr.encode_text(query, self.hrr_dim)
                hrr_sim = (hrr.similarity(query_vec, fact_vec) + 1.0) / 2.0
            else:
                hrr_sim = 0.5

            relevance = (self.fts_weight * fts_score
                         + self.jaccard_weight * jaccard
                         + self.hrr_weight * hrr_sim)
            fact["score"] = relevance * fact["trust_score"]
            fact.pop("hrr_vector", None)
            scored.append(fact)

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def _entity_linked_facts(self, entities: List[str], category: Optional[str],
                             require_all: bool, limit: int) -> List[Dict]:
        targets = [entity.strip().lower() for entity in entities if entity.strip()]
        if not targets:
            return []

        placeholders = ",".join("?" * len(targets))
        category_clause = "AND f.category = ?" if category else ""
        params: list = list(targets)
        if category:
            params.append(category)

        having_clause = ""
        if require_all:
            having_clause = "HAVING COUNT(DISTINCT lower(e.name)) = ?"
            params.append(len(set(targets)))
        params.append(limit)

        rows = self.store._conn.execute(
            f"""
            SELECT f.fact_id, f.content, f.category, f.tags, f.trust_score,
                   f.retrieval_count, f.helpful_count, f.created_at, f.updated_at,
                   COUNT(DISTINCT lower(e.name)) AS entity_matches
            FROM facts f
            JOIN fact_entities fe ON fe.fact_id = f.fact_id
            JOIN entities e ON e.entity_id = fe.entity_id
            WHERE lower(e.name) IN ({placeholders})
              {category_clause}
            GROUP BY f.fact_id
            {having_clause}
            ORDER BY entity_matches DESC, f.trust_score DESC, f.fact_id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()

        target_count = max(len(set(targets)), 1)
        results = []
        for row in rows:
            fact = dict(row)
            matches = fact.pop("entity_matches")
            fact["score"] = (matches / target_count) * fact["trust_score"]
            results.append(fact)
        return results

    def probe(self, entity: str, category: Optional[str] = None,
              limit: int = 10) -> List[Dict]:
        """Compositional entity query using HRR algebra."""
        linked = self._entity_linked_facts([entity], category, require_all=True, limit=limit)
        if linked:
            return linked
        if not hrr.HAS_NUMPY:
            return self.search(entity, category=category, limit=limit)

        conn = self.store._conn
        role_entity = hrr.encode_atom("__hrr_role_entity__", self.hrr_dim)
        entity_vec = hrr.encode_atom(entity.lower(), self.hrr_dim)
        probe_key = hrr.bind(entity_vec, role_entity)
        role_content = hrr.encode_atom("__hrr_role_content__", self.hrr_dim)

        where = "WHERE hrr_vector IS NOT NULL"
        params: list = []
        if category:
            where += " AND category = ?"
            params.append(category)

        rows = conn.execute(
            f"SELECT fact_id, content, category, tags, trust_score, "
            f"retrieval_count, helpful_count, created_at, updated_at, hrr_vector "
            f"FROM facts {where}", params,
        ).fetchall()

        if not rows:
            return self.search(entity, category=category, limit=limit)

        scored = []
        for row in rows:
            fact = dict(row)
            fact_vec = hrr.bytes_to_phases(fact.pop("hrr_vector"))
            residual = hrr.unbind(fact_vec, probe_key)
            content_vec = hrr.bind(hrr.encode_text(fact["content"], self.hrr_dim), role_content)
            sim = hrr.similarity(residual, content_vec)
            fact["score"] = (sim + 1.0) / 2.0 * fact["trust_score"]
            scored.append(fact)

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def reason(self, entities: List[str], category: Optional[str] = None,
               limit: int = 10) -> List[Dict]:
        """Multi-entity compositional query -- vector-space JOIN."""
        linked = self._entity_linked_facts(entities, category, require_all=True, limit=limit)
        if linked:
            return linked
        if not hrr.HAS_NUMPY or not entities:
            return self.search(" ".join(entities), category=category, limit=limit)

        conn = self.store._conn
        role_entity = hrr.encode_atom("__hrr_role_entity__", self.hrr_dim)
        role_content = hrr.encode_atom("__hrr_role_content__", self.hrr_dim)

        probe_keys = []
        for entity in entities:
            ev = hrr.encode_atom(entity.lower(), self.hrr_dim)
            probe_keys.append(hrr.bind(ev, role_entity))

        where = "WHERE hrr_vector IS NOT NULL"
        params: list = []
        if category:
            where += " AND category = ?"
            params.append(category)

        rows = conn.execute(
            f"SELECT fact_id, content, category, tags, trust_score, "
            f"retrieval_count, helpful_count, created_at, updated_at, hrr_vector "
            f"FROM facts {where}", params,
        ).fetchall()

        if not rows:
            return self.search(" ".join(entities), category=category, limit=limit)

        scored = []
        for row in rows:
            fact = dict(row)
            fact_vec = hrr.bytes_to_phases(fact.pop("hrr_vector"))
            entity_scores = []
            for pk in probe_keys:
                residual = hrr.unbind(fact_vec, pk)
                sim = hrr.similarity(residual, role_content)
                entity_scores.append(sim)
            min_sim = min(entity_scores)
            fact["score"] = (min_sim + 1.0) / 2.0 * fact["trust_score"]
            scored.append(fact)

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def contradict(self, category: Optional[str] = None, threshold: float = 0.3,
                   limit: int = 10) -> List[Dict]:
        """Find potentially contradictory facts via entity overlap + content divergence."""
        if not hrr.HAS_NUMPY:
            return []

        conn = self.store._conn
        where = "WHERE f.hrr_vector IS NOT NULL"
        params: list = []
        if category:
            where += " AND f.category = ?"
            params.append(category)

        rows = conn.execute(
            f"SELECT f.fact_id, f.content, f.category, f.tags, f.trust_score, "
            f"f.created_at, f.updated_at, f.hrr_vector FROM facts f {where}", params,
        ).fetchall()

        if len(rows) < 2:
            return []

        # Cap at 500 to avoid O(n^2) explosion
        if len(rows) > 500:
            rows = sorted(rows, key=lambda r: r["updated_at"] or r["created_at"], reverse=True)[:500]

        fact_entities: Dict[int, set] = {}
        for row in rows:
            fid = row["fact_id"]
            ent_rows = conn.execute(
                "SELECT e.name FROM entities e "
                "JOIN fact_entities fe ON fe.entity_id = e.entity_id "
                "WHERE fe.fact_id = ?", (fid,),
            ).fetchall()
            fact_entities[fid] = {r["name"].lower() for r in ent_rows}

        facts = [dict(r) for r in rows]
        contradictions = []
        for i in range(len(facts)):
            for j in range(i + 1, len(facts)):
                f1, f2 = facts[i], facts[j]
                ents1 = fact_entities.get(f1["fact_id"], set())
                ents2 = fact_entities.get(f2["fact_id"], set())
                if not ents1 or not ents2:
                    continue
                overlap = len(ents1 & ents2) / len(ents1 | ents2) if (ents1 | ents2) else 0.0
                if overlap < 0.3:
                    continue
                v1 = hrr.bytes_to_phases(f1["hrr_vector"])
                v2 = hrr.bytes_to_phases(f2["hrr_vector"])
                content_sim = hrr.similarity(v1, v2)
                score = overlap * (1.0 - (content_sim + 1.0) / 2.0)
                if score >= threshold:
                    f1c = {k: v for k, v in f1.items() if k != "hrr_vector"}
                    f2c = {k: v for k, v in f2.items() if k != "hrr_vector"}
                    contradictions.append({
                        "fact_a": f1c, "fact_b": f2c,
                        "entity_overlap": round(overlap, 3),
                        "content_similarity": round(content_sim, 3),
                        "contradiction_score": round(score, 3),
                        "shared_entities": sorted(ents1 & ents2),
                    })

        contradictions.sort(key=lambda x: x["contradiction_score"], reverse=True)
        return contradictions[:limit]

    # -- Internal helpers ----------------------------------------------------

    def _fts_candidates(self, query: str, category: Optional[str],
                        min_trust: float, limit: int) -> List[Dict]:
        match_query = _fts5_match_query(query)
        if not match_query:
            return []
        conn = self.store._conn
        params: list = [match_query]
        where_parts = ["facts_fts MATCH ?"]
        if category:
            where_parts.append("f.category = ?")
            params.append(category)
        where_parts.append("f.trust_score >= ?")
        params.append(min_trust)
        params.append(limit)

        sql = f"""
            SELECT f.*, facts_fts.rank as fts_rank_raw
            FROM facts_fts
            JOIN facts f ON f.fact_id = facts_fts.rowid
            WHERE {' AND '.join(where_parts)}
            ORDER BY facts_fts.rank LIMIT ?
        """
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return []

        raw_ranks = [abs(row["fts_rank_raw"]) for row in rows]
        max_rank = max(raw_ranks) if raw_ranks else 1.0
        max_rank = max(max_rank, 1e-6)
        results = []
        for row, rr in zip(rows, raw_ranks):
            fact = dict(row)
            fact.pop("fts_rank_raw", None)
            fact["fts_rank"] = rr / max_rank
            results.append(fact)
        return results

    @staticmethod
    def _tokenize(text: str) -> set:
        if not text:
            return set()
        return {w.strip(".,;:!?\"'()[]{}#@<>") for w in text.lower().split()} - {""}

    @staticmethod
    def _jaccard(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# Convenience: backward-compatible IndexManager wrapper
# ---------------------------------------------------------------------------

class IndexManager:
    """Backward-compatible wrapper around MemoryStore for existing callers."""

    def __init__(self, client: Any = None, storage: Any = None) -> None:
        # client is no longer needed (no API calls), kept for signature compat
        if isinstance(storage, MemoryStore):
            self._store = storage
        else:
            self._store = MemoryStore()
        self._retriever = FactRetriever(self._store)

    def index_file(self, file_path: str, content: str) -> None:
        """Index a code file as a fact, delegating parsing and storage to MemoryStore."""
        self._store.index_code(file_path, content)

    def index_failure(self, task_id: str, description: str, failure_msg: str) -> None:
        """Index a failed improvement attempt."""
        text = f"[failure] {description}: {failure_msg[:300]}"
        self._store.add_fact(text, category="failure", tags=task_id)

    def index_success(self, task_id: str, description: str, details: str = "") -> None:
        """Index a successful improvement."""
        text = f"[success] {description}"
        if details:
            text += f": {details[:300]}"
        self._store.add_fact(text, category="success", tags=task_id)

    def retrieve_relevant_context(self, query: str, limit: int = 5) -> str:
        """Search memory for relevant past experiences."""
        results = self._retriever.search(query, limit=limit)
        if not results:
            return ""
        parts = ["### Relevant Past Context (Holographic Memory)"]
        for fact in results:
            if fact.get("score", 0) < 0.1:
                continue
            parts.append(
                f"- [{fact['category']}] {fact['content'][:200]} "
                f"(trust: {fact['trust_score']:.2f})"
            )
        return "\n".join(parts) if len(parts) > 1 else ""

    def record_outcome_feedback(self, description: str, success: bool) -> None:
        """Find facts related to a task and adjust trust based on outcome."""
        try:
            results = self._retriever.search(description, limit=3)
            for fact in results:
                if fact.get("score", 0) > 0.2:
                    self._store.record_feedback(fact["fact_id"], helpful=success)
        except Exception:
            log.debug("Failed to record outcome feedback")

    def run_hygiene(self) -> int:
        """Run contradiction detection and prune low-trust facts. Returns count removed."""
        removed = 0

        for item in self._retriever.contradict():
            fact_a_id = item["fact_a"]["fact_id"]
            fact_b_id = item["fact_b"]["fact_id"]
            if fact_a_id == fact_b_id:
                continue

            with self._store._lock:
                rows = self._store._conn.execute(
                    "SELECT fact_id, trust_score, created_at FROM facts "
                    "WHERE fact_id IN (?, ?)",
                    (fact_a_id, fact_b_id),
                ).fetchall()
            facts = {row["fact_id"]: dict(row) for row in rows}
            if fact_a_id not in facts or fact_b_id not in facts:
                continue

            fact_a = facts[fact_a_id]
            fact_b = facts[fact_b_id]
            if fact_a["trust_score"] < fact_b["trust_score"]:
                loser = fact_a
            elif fact_b["trust_score"] < fact_a["trust_score"]:
                loser = fact_b
            elif (fact_a["created_at"] or "", fact_a["fact_id"]) <= (
                fact_b["created_at"] or "",
                fact_b["fact_id"],
            ):
                loser = fact_a
            else:
                loser = fact_b

            new_trust = _clamp_trust(loser["trust_score"] + _UNHELPFUL_DELTA)
            if new_trust < 0.05:
                if self._store.remove_fact(loser["fact_id"]):
                    removed += 1
            else:
                self._store.update_fact(loser["fact_id"], trust_delta=_UNHELPFUL_DELTA)

        # Prune facts with very low trust
        low_trust = self._store.list_facts(min_trust=0.0, limit=100)
        for fact in low_trust:
            if fact["trust_score"] <= 0.05 and fact["retrieval_count"] == 0:
                self._store.remove_fact(fact["fact_id"])
                removed += 1
        return removed
