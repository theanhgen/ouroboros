"""Retrieval has to return something, and not just anything.

Memory was written to and never read for five months. PR #82 fixed the crash
(`fts5: syntax error near "#"`) and left the semantics that made every real
query empty: `_fts5_match_query` joined tokens with spaces, which FTS5 reads as
implicit AND, so a fact had to contain *every* token of a ~120-token codebase
summary. Measured on the production database before this change: a two-token
query returned 3 rows, a six-token query 0, the real production query 0.

Switching to OR fixes recall and opens a hole, because `fts_rank` is normalised
against its own batch -- the best row of any batch scores 1.0 no matter how
weak. These tests pin both halves: recall degrades gracefully with query
length, and a fact that merely brushes the query still cannot get through.
"""

import sqlite3

import pytest

from ouroboros.memory import MemoryStore, FactRetriever, _fts5_match_query


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(db_path=tmp_path / "memory.db")
    s.add_fact("pytest output parser drops the summary line on empty runs",
               category="code", tags="test_runner parsing")
    s.add_fact("git status porcelain parsing mishandles renamed files",
               category="code", tags="backends git")
    s.add_fact("holographic bundle rejects vectors of mismatched dimension",
               category="code", tags="holographic")
    return s


# --------------------------------------------------------- the query compiler

class TestMatchQuery:
    def test_and_is_still_the_default(self):
        """Callers that want an exact conjunction must keep getting one."""
        assert _fts5_match_query("alpha beta") == '"alpha" "beta"'

    def test_or_mode_joins_with_or(self):
        assert _fts5_match_query("alpha beta", match_all=False) == '"alpha" OR "beta"'

    def test_punctuation_is_still_quoted_not_interpreted(self):
        """The whole reason the helper exists -- see PR #82.

        Asserted in AND mode, which is where literal quoting is the safety
        property: OR mode additionally drops stopwords and tokens with no
        alphanumeric character, so "AND" and "*" do not survive it.
        """
        out = _fts5_match_query("issue:123 AND *")
        assert '"issue:123"' in out and '"AND"' in out and '"*"' in out

    def test_empty_text_compiles_to_nothing(self):
        assert _fts5_match_query("   ") == ""

    def test_max_tokens_caps_the_query(self):
        q = "alpha bravo charlie delta echo foxtrot golf"
        assert _fts5_match_query(q, match_all=False, max_tokens=3).count(" OR ") == 2

    def test_capping_drops_stopwords_first(self):
        out = _fts5_match_query("the memory module for a codebase",
                                match_all=False, max_tokens=3)
        for stop in ('"the"', '"for"', '"a"'):
            assert stop not in out

    def test_capping_is_deterministic(self):
        q = "improve the retrieval semantics of the memory subsystem"
        a = _fts5_match_query(q, match_all=False, max_tokens=4)
        b = _fts5_match_query(q, match_all=False, max_tokens=4)
        assert a == b

    def test_an_all_stopword_query_compiles_to_nothing(self):
        """It used to fall back to the raw tokens, which was wrong.

        OR-ing "the" and "for" across the table matches nearly every fact, and
        those matches then satisfy an overlap gate on stopwords alone. A query
        with no informative word is not about anything; no results is the
        honest answer.
        """
        assert _fts5_match_query("the and for", match_all=False, max_tokens=2) == ""
        assert _fts5_match_query("the and for") != "", "AND mode is unchanged"


# ------------------------------------------------------------------- recall

class TestRecall:
    def test_the_regression_that_started_this(self, store):
        """Six informative tokens returned 0 rows under AND, on real data."""
        r = FactRetriever(store)
        assert r.search("pytest output parser summary line empty", min_trust=0.0)

    def test_a_long_query_still_retrieves(self, store):
        """The production shape: a codebase summary, ~120 tokens. Under AND this
        was structurally incapable of matching anything, ever."""
        summary = ("# Ouroboros Codebase Summary\n\n" + " ".join(
            ["module"] * 40) + " pytest output parser summary line")
        assert FactRetriever(store).search(summary, min_trust=0.0)

    def test_a_fact_matches_its_own_content(self, store):
        r = FactRetriever(store)
        for fact in store.list_facts(limit=10):
            assert r.search(fact["content"], min_trust=0.0), fact["content"]


# ------------------------------------------------------- the admission gate

class TestOverlapGate:
    def test_one_incidental_token_is_not_enough(self, store):
        """agy's finding, as a fixture.

        'parser' hits the pytest fact and nothing else about the query is
        related. Under OR with no gate that fact becomes its batch's maximum,
        scores ~0.275, and passes both the 0.1 retrieval cutoff and the 0.2
        trust-feedback threshold.
        """
        hits = FactRetriever(store).search(
            "parser", min_trust=0.0)
        # A single-token query is exempt by design; the gate is about long ones.
        assert hits, "a deliberate one-word lookup must still work"

        noise = FactRetriever(store).search(
            "kubernetes ingress certificate rotation parser", min_trust=0.0)
        assert noise == [], "one incidental token must not admit a fact"

    def test_two_shared_tokens_do_get_through(self, store):
        assert FactRetriever(store).search(
            "kubernetes ingress porcelain renamed", min_trust=0.0)

    def test_the_gate_is_configurable(self, store):
        loose = FactRetriever(store, min_overlap=1)
        assert loose.search("kubernetes ingress certificate parser", min_trust=0.0)

    def test_nothing_relevant_returns_nothing(self, store):
        assert FactRetriever(store).search(
            "kubernetes ingress certificate rotation", min_trust=0.0) == []


# ------------------------------------------------------------ instrumentation

class TestRetrievalCount:
    def test_returned_facts_are_counted(self, store):
        r = FactRetriever(store)
        hits = r.search("pytest output parser summary", min_trust=0.0)
        assert hits
        after = {f["fact_id"]: f["retrieval_count"] for f in store.list_facts(limit=10)}
        for fact in hits:
            assert after[fact["fact_id"]] == 1

    def test_facts_that_lost_the_rerank_are_not_counted(self, store):
        """Counting candidates rather than results would overstate every fact
        the query merely brushed against."""
        r = FactRetriever(store)
        hits = r.search("pytest output parser summary", min_trust=0.0)
        returned = {f["fact_id"] for f in hits}
        for fact in store.list_facts(limit=10):
            if fact["fact_id"] not in returned:
                assert fact["retrieval_count"] == 0

    def test_counting_repeats(self, store):
        r = FactRetriever(store)
        for _ in range(3):
            r.search("pytest output parser summary", min_trust=0.0)
        counts = [f["retrieval_count"] for f in store.list_facts(limit=10)]
        assert max(counts) == 3


# ------------------------------------------------------------------- hygiene

class TestPruneGuard:
    """Hygiene keys on helpful_count, not retrieval_count.

    The old guard spared any fact with retrieval_count == 0, which was every
    fact while the counter never moved -- the clause was inert. Now that
    retrieval counts, keeping it would invert the intent exactly: a discredited
    fact retrieved once becomes permanently unprunable, so the more often a bad
    fact is surfaced the safer it gets.
    """

    def _discredit(self, store, fact_id):
        for _ in range(40):
            store.record_feedback(fact_id, helpful=False)

    def _find(self, store, fact_id):
        for f in store.list_facts(min_trust=0.0, limit=100):
            if f["fact_id"] == fact_id:
                return f
        return None

    def test_retrieval_alone_does_not_immunise(self, store):
        fid = store.list_facts(limit=1)[0]["fact_id"]
        self._discredit(store, fid)
        store.note_retrieved([fid])
        fact = self._find(store, fid)
        assert fact["trust_score"] <= 0.05
        assert fact["retrieval_count"] > 0
        assert fact["helpful_count"] == 0, (
            "this is the fact the old guard would have spared forever")

    def test_being_useful_is_what_earns_the_reprieve(self, store):
        fid = store.list_facts(limit=1)[0]["fact_id"]
        store.record_feedback(fid, helpful=True)
        self._discredit(store, fid)
        fact = self._find(store, fid)
        assert fact["trust_score"] <= 0.05
        assert fact["helpful_count"] > 0


# --------------------------------------------------------------- containment

class TestFtsErrorsDoNotCrashTheCycle:
    """Where failures are swallowed, and where they must not be.

    `FactRetriever.search` and `MemoryStore.search_facts` both propagate: a
    dropped table masked as "no results" is how a broken index goes unnoticed.
    `IndexManager.retrieve_relevant_context` is the exception, because
    improvement.py calls it bare on an unattended cycle and memory is an
    optimisation for that run, not a precondition.
    """

    class _ExplodingConn:
        """sqlite3.Connection.execute is read-only, so the connection is
        swapped rather than patched."""

        def __init__(self, real, exc):
            self._real, self._exc = real, exc

        def execute(self, *_a, **_k):
            raise self._exc

        def __getattr__(self, name):
            return getattr(self._real, name)

    def test_the_retriever_still_propagates(self, store, monkeypatch):
        monkeypatch.setattr(store, "_conn", self._ExplodingConn(
            store._conn, sqlite3.OperationalError("no such table: facts_fts")))
        with pytest.raises(sqlite3.OperationalError):
            FactRetriever(store).search("pytest parser summary", min_trust=0.0)

    def test_the_cycle_boundary_degrades_to_empty(self, store, monkeypatch):
        from ouroboros.memory import IndexManager
        manager = IndexManager(storage=store)
        monkeypatch.setattr(store, "_conn", self._ExplodingConn(
            store._conn, sqlite3.OperationalError("no such table: facts_fts")))
        assert manager.retrieve_relevant_context("pytest parser summary") == ""

    def test_the_degradation_is_not_silent(self, store, monkeypatch, caplog):
        """A permanently failing query must not look like an empty database --
        that confusion is what kept this subsystem broken for five months."""
        import logging

        from ouroboros.memory import IndexManager
        manager = IndexManager(storage=store)
        monkeypatch.setattr(store, "_conn", self._ExplodingConn(
            store._conn, sqlite3.OperationalError("malformed MATCH")))
        with caplog.at_level(logging.WARNING):
            manager.retrieve_relevant_context("pytest parser summary")
        assert any("retrieval" in r.message.lower() for r in caplog.records)


class TestTokenSelection:
    """Every case here was a real defect in the first cut of this change.

    Found by an adversarial review pass, all four reproduced before being fixed.
    They share a shape: OR mode makes token *selection* load-bearing in a way
    AND mode never did, because under AND a junk token only narrowed the result
    and under OR it widens it.
    """

    def test_a_repeated_word_cannot_take_every_slot(self):
        """A codebase summary naming one module thirty times compiled to that
        word six times and starved out every real keyword."""
        q = "# Summary\n" + "improvement " * 30 + "parser holographic"
        out = _fts5_match_query(q, match_all=False, max_tokens=6)
        assert out.count('"improvement"') == 1
        assert '"parser"' in out and '"holographic"' in out

    def test_markdown_rules_are_not_keywords(self):
        """`-----` survives punctuation stripping, is in no stopword list, and
        is long -- so length-ranked selection picked it over every real word."""
        out = _fts5_match_query(
            "------------------------ ==================== parser memory",
            match_all=False, max_tokens=3)
        assert "-" not in out and "=" not in out
        assert '"parser"' in out and '"memory"' in out

    def test_stopwords_are_filtered_even_without_capping(self):
        """The filter used to be nested inside the capping branch, so a short
        query kept its stopwords -- and `OR "the"` matches nearly everything."""
        out = _fts5_match_query("what is the parser for", match_all=False, max_tokens=12)
        assert out == '"parser"'

    def test_and_mode_keeps_every_token(self):
        """Filtering belongs to OR mode alone. Under AND a stopword is nearly
        free, and callers doing exact literal lookups depend on it."""
        assert _fts5_match_query("what is the parser for").count(" ") == 4


class TestGateExemption:
    def test_a_stopword_only_query_matches_nothing(self, store):
        """The gate used to fall back to raw tokens when nothing was
        informative, which let stopwords alone satisfy it."""
        assert FactRetriever(store).search("the and for is", min_trust=0.0) == []

    def test_a_single_keyword_query_still_works(self, store):
        """Requiring two overlaps of a one-word question would return nothing,
        which is not the failure the gate exists to prevent."""
        assert FactRetriever(store).search("porcelain", min_trust=0.0)

    def test_a_rich_query_still_needs_two(self, store):
        assert FactRetriever(store).search(
            "kubernetes ingress certificate rotation porcelain", min_trust=0.0) == []


class TestUnattendedBoundaryIsBroad:
    def test_a_non_database_failure_also_degrades(self, store, monkeypatch):
        """`search` runs numpy vector maths, so it can raise things that are not
        DatabaseError -- and those would still have aborted the cycle."""
        from ouroboros.memory import IndexManager
        manager = IndexManager(storage=store)

        def boom(*_a, **_k):
            raise ValueError("operands could not be broadcast together")

        monkeypatch.setattr(manager._retriever, "search", boom)
        assert manager.retrieve_relevant_context("pytest parser summary") == ""
