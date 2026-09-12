"""Tests for holographic memory and AST-based code indexing."""

import sqlite3
import tempfile
from pathlib import Path
import pytest
import textwrap

from ouroboros import holographic as hrr
from ouroboros.memory import FactRetriever, IndexManager, MemoryStore


@pytest.fixture
def temp_store(tmp_path):
    db_path = tmp_path / "test_memory.db"
    store = MemoryStore(db_path=db_path)
    yield store
    store.close()


def _fact_by_id(store, fact_id):
    row = store._conn.execute(
        "SELECT fact_id, trust_score, retrieval_count, created_at "
        "FROM facts WHERE fact_id = ?",
        (fact_id,),
    ).fetchone()
    return dict(row) if row else None


def _set_fact_state(store, fact_id, **fields):
    assignments = [f"{name} = ?" for name in fields]
    store._conn.execute(
        f"UPDATE facts SET {', '.join(assignments)} WHERE fact_id = ?",
        [*fields.values(), fact_id],
    )
    store._conn.commit()


def _set_fact_hrr(store, fact_id, content, entities):
    vector = hrr.encode_fact(content, entities, store.hrr_dim)
    store._conn.execute(
        "UPDATE facts SET hrr_vector = ? WHERE fact_id = ?",
        (hrr.phases_to_bytes(vector), fact_id),
    )
    store._conn.commit()


def test_memory_store_index_code_returns_fact_ids(temp_store):
    code_content = textwrap.dedent('''
        """Module docs."""

        def helper(value: int) -> str:
            """Helper docs."""
            return str(value)
    ''').strip()

    fact_ids = temp_store.index_code("src/helper.py", code_content)
    facts = temp_store.list_facts(category="code")
    fact_contents = {fact["content"] for fact in facts}
    stored_vectors = temp_store._conn.execute(
        "SELECT fact_id, hrr_vector FROM facts WHERE category = ?",
        ("code",),
    ).fetchall()

    assert isinstance(fact_ids, list)
    assert fact_ids
    assert all(isinstance(fact_id, int) for fact_id in fact_ids)
    assert {fact["fact_id"] for fact in facts} == set(fact_ids)
    assert {row["fact_id"] for row in stored_vectors} == set(fact_ids)
    if hrr.HAS_NUMPY:
        assert all(row["hrr_vector"] is not None for row in stored_vectors)
    assert {fact["tags"] for fact in facts} == {"src/helper.py"}
    assert "[code] src/helper.py: module docstring: Module docs." in fact_contents
    assert "[code] src/helper.py: def helper(value: int) -> str" in fact_contents
    assert "[code] src/helper.py: def helper docstring: Helper docs." in fact_contents


def test_index_file_python_ast(temp_store):
    manager = IndexManager(storage=temp_store)

    code_content = textwrap.dedent('''
        """Module-level documentation."""

        class OuterClass:
            """Outer class doc."""
            
            def outer_method(self, a: int, b: str = "default") -> bool:
                """Method doc."""
                return True

            class InnerClass:
                pass

        async def standalone_async_func(x):
            """Async doc."""
            pass
    ''').strip()

    file_path = "src/example.py"
    manager.index_file(file_path, code_content)

    # Let's search for the indexed facts and check if they are correct
    facts = temp_store.list_facts(category="code")
    fact_contents = {f["content"] for f in facts}

    expected_facts = {
        "[code] src/example.py: module docstring: Module-level documentation.",
        "[code] src/example.py: class OuterClass",
        "[code] src/example.py: class OuterClass docstring: Outer class doc.",
        "[code] src/example.py: class OuterClass.InnerClass",
        "[code] src/example.py: def OuterClass.outer_method(self, a: int, b: str='default') -> bool",
        "[code] src/example.py: def OuterClass.outer_method docstring: Method doc.",
        "[code] src/example.py: async def standalone_async_func(x)",
        "[code] src/example.py: def standalone_async_func docstring: Async doc.",
    }

    # Verify that all expected facts are in fact_contents
    for expected in expected_facts:
        assert expected in fact_contents, f"Missing expected fact: {expected}"

    assert len(fact_contents) == len(expected_facts)


def test_index_file_fallback_syntax_error(temp_store):
    manager = IndexManager(storage=temp_store)

    # Python file with syntax error
    code_content = "class IncompleteOuter:\n  def method(self"
    file_path = "src/error.py"
    manager.index_file(file_path, code_content)

    facts = temp_store.list_facts(category="code")
    assert len(facts) == 1
    assert facts[0]["content"] == f"[code] src/error.py: {code_content}"


def test_index_file_fallback_non_python(temp_store):
    manager = IndexManager(storage=temp_store)

    # Non-python file
    text_content = "This is some plain text document."
    file_path = "docs/readme.txt"
    manager.index_file(file_path, text_content)

    facts = temp_store.list_facts(category="code")
    assert len(facts) == 1
    assert facts[0]["content"] == f"[code] docs/readme.txt: {text_content}"


def test_index_file_fallback_empty_python(temp_store):
    manager = IndexManager(storage=temp_store)

    # Empty python file or python file with no structure (no docstring, no classes, no functions)
    code_content = ""
    file_path = "src/empty.py"
    manager.index_file(file_path, code_content)

    facts = temp_store.list_facts(category="code")
    assert len(facts) == 1
    assert facts[0]["content"] == f"[code] src/empty.py:"


def test_memory_store_crud_entities_search_and_feedback(temp_store):
    fact_id = temp_store.add_fact(
        'Ada Lovelace wrote "Analytical Engine" notes for '
        "'Bernoulli Numbers'.",
        category="history",
        tags="computing math",
    )

    assert temp_store.fact_count() == 1
    stored = temp_store.list_facts(category="history")
    assert stored[0]["fact_id"] == fact_id
    assert stored[0]["content"].startswith("Ada Lovelace wrote")
    assert stored[0]["tags"] == "computing math"
    assert stored[0]["trust_score"] == pytest.approx(0.5)

    linked_entities = {
        row["name"]
        for row in temp_store._conn.execute(
            "SELECT e.name FROM entities e "
            "JOIN fact_entities fe ON fe.entity_id = e.entity_id "
            "WHERE fe.fact_id = ?",
            (fact_id,),
        )
    }
    assert linked_entities == {
        "Ada Lovelace",
        "Analytical Engine",
        "Bernoulli Numbers",
    }

    results = temp_store.search_facts("math", category="history")
    assert [result["fact_id"] for result in results] == [fact_id]
    assert temp_store.list_facts()[0]["retrieval_count"] == 1

    helpful = temp_store.record_feedback(fact_id, helpful=True)
    assert helpful["old_trust"] == pytest.approx(0.5)
    assert helpful["new_trust"] == pytest.approx(0.55)
    unhelpful = temp_store.record_feedback(fact_id, helpful=False)
    assert unhelpful["new_trust"] == pytest.approx(0.45)
    after_feedback = temp_store.list_facts()[0]
    assert after_feedback["helpful_count"] == 1
    assert after_feedback["trust_score"] == pytest.approx(0.45)

    assert temp_store.update_fact(
        fact_id,
        content='Grace Hopper documented "FLOW MATIC" compilers.',
        category="software",
        tags="compiler",
        trust_delta=0.4,
    )
    updated = temp_store.search_facts("compilers", category="software", min_trust=0.8)
    assert [result["fact_id"] for result in updated] == [fact_id]
    assert temp_store.search_facts("Bernoulli", category="software") == []

    updated_entities = {
        row["name"]
        for row in temp_store._conn.execute(
            "SELECT e.name FROM entities e "
            "JOIN fact_entities fe ON fe.entity_id = e.entity_id "
            "WHERE fe.fact_id = ?",
            (fact_id,),
        )
    }
    assert updated_entities == {"Grace Hopper", "FLOW MATIC"}

    assert temp_store.remove_fact(fact_id)
    assert temp_store.fact_count() == 0
    assert temp_store.search_facts("compilers", category="software") == []
    assert not temp_store.update_fact(fact_id, trust_delta=0.1)
    assert not temp_store.remove_fact(fact_id)
    with pytest.raises(KeyError):
        temp_store.record_feedback(fact_id, helpful=True)


def test_prune_to_snr_capacity_limit(tmp_path):
    store = MemoryStore(db_path=tmp_path / "snr.db", hrr_dim=16)
    try:
        for i in range(6):
            store.add_fact(f"snr capacity fact {i}", category="test")

        assert store.prune_to_snr(category="test", min_snr=2.0) == 2

        count = store._conn.execute(
            "SELECT COUNT(*) FROM facts WHERE category = ?", ("test",)
        ).fetchone()[0]
        assert count == 4
    finally:
        store.close()


def test_prune_to_snr_selection_order(tmp_path):
    store = MemoryStore(db_path=tmp_path / "snr_order.db", hrr_dim=16)
    try:
        low_trust = store.add_fact("prune lowest trust", category="order")
        low_helpful = store.add_fact("prune lowest helpful count", category="order")
        older = store.add_fact("prune older equal fact", category="order")
        newer = store.add_fact("keep newer equal fact", category="order")
        keep_a = store.add_fact("keep trusted fact a", category="order")
        keep_b = store.add_fact("keep trusted fact b", category="order")
        keep_c = store.add_fact("keep trusted fact c", category="order")

        _set_fact_state(
            store, low_trust, trust_score=0.10, helpful_count=9,
            created_at="2024-01-05 00:00:00"
        )
        _set_fact_state(
            store, low_helpful, trust_score=0.20, helpful_count=0,
            created_at="2024-01-06 00:00:00"
        )
        _set_fact_state(
            store, older, trust_score=0.20, helpful_count=1,
            created_at="2024-01-01 00:00:00"
        )
        _set_fact_state(
            store, newer, trust_score=0.20, helpful_count=1,
            created_at="2024-01-02 00:00:00"
        )
        for fact_id in (keep_a, keep_b, keep_c):
            _set_fact_state(store, fact_id, trust_score=0.80, helpful_count=0)

        assert store.prune_to_snr(category="order", min_snr=2.0) == 3

        remaining = {
            row["fact_id"]
            for row in store._conn.execute("SELECT fact_id FROM facts")
        }
        assert {low_trust, low_helpful, older}.isdisjoint(remaining)
        assert {newer, keep_a, keep_b, keep_c} <= remaining
    finally:
        store.close()


def test_prune_to_snr_all_categories(tmp_path):
    store = MemoryStore(db_path=tmp_path / "snr_all.db", hrr_dim=16)
    try:
        for category, total in (("cat_a", 5), ("cat_b", 6), ("cat_c", 3)):
            for i in range(total):
                store.add_fact(f"{category} fact {i}", category=category)

        assert store.prune_to_snr(min_snr=2.0) == 3

        counts = dict(
            store._conn.execute(
                "SELECT category, COUNT(*) FROM facts GROUP BY category"
            ).fetchall()
        )
        assert counts == {"cat_a": 4, "cat_b": 4, "cat_c": 3}
    finally:
        store.close()


def test_prune_to_snr_under_capacity_no_op(tmp_path):
    store = MemoryStore(db_path=tmp_path / "snr_noop.db", hrr_dim=16)
    try:
        fact_ids = [
            store.add_fact(f"under capacity fact {i}", category="small")
            for i in range(3)
        ]

        assert store.prune_to_snr(category="small", min_snr=2.0) == 0

        remaining = [
            row["fact_id"]
            for row in store._conn.execute(
                "SELECT fact_id FROM facts WHERE category = ? ORDER BY fact_id",
                ("small",),
            )
        ]
        assert remaining == fact_ids
    finally:
        store.close()


@pytest.mark.parametrize("min_snr", [0, -1.0])
def test_prune_to_snr_invalid_min_snr(temp_store, min_snr):
    with pytest.raises(ValueError, match="min_snr must be positive"):
        temp_store.prune_to_snr(min_snr=min_snr)


def test_prune_to_snr_updates_memory_bank(tmp_path):
    if not hrr.HAS_NUMPY:
        pytest.skip("NumPy is required for memory bank assertions")

    store = MemoryStore(db_path=tmp_path / "snr_bank.db", hrr_dim=16)
    try:
        for i in range(6):
            store.add_fact(f"memory bank fact {i}", category="bank")

        before = store._conn.execute(
            "SELECT fact_count FROM memory_banks WHERE bank_name = ?",
            ("cat:bank",),
        ).fetchone()
        assert before["fact_count"] == 6

        assert store.prune_to_snr(category="bank", min_snr=2.0) == 2

        after = store._conn.execute(
            "SELECT fact_count FROM memory_banks WHERE bank_name = ?",
            ("cat:bank",),
        ).fetchone()
        assert after["fact_count"] == 4
    finally:
        store.close()


@pytest.mark.parametrize(
    "query",
    [
        "col:val",        # unknown column
        'foo"bar',        # unbalanced double quote
        '"unclosed',      # unterminated phrase
        "NEAR/",          # malformed NEAR
        "*",              # bare special-query prefix
        "***",
        '" "',
    ],
)
def test_search_facts_no_hits_for_fts5_syntax_text(temp_store, query):
    """FTS5 syntax characters in user text must not escape as an exception."""
    temp_store.add_fact("the cat sat on the mat", category="test")

    assert temp_store.search_facts(query) == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("cat:", "the cat sat on the mat"),          # punctuation is a separator
        ("issue:123", "see issue:123 for details"),  # colon is not a column filter
        ("http://example.com", "visit http://example.com now"),
        ("user's manual", "the user's manual"),
        ("a AND", "a AND b are both listed"),        # AND is not an operator
    ],
)
def test_search_facts_treats_query_as_literal_text(temp_store, query, expected):
    """Text with FTS5 metacharacters searches literally instead of failing."""
    for content in (
        "the cat sat on the mat",
        "see issue:123 for details",
        "visit http://example.com now",
        "the user's manual",
        "a AND b are both listed",
    ):
        temp_store.add_fact(content, category="test")

    assert [r["content"] for r in temp_store.search_facts(query)] == [expected]


def test_search_facts_handles_embedded_nul(temp_store):
    """U+0000 truncates the MATCH expression however it is quoted."""
    temp_store.add_fact("the cat sat on the mat", category="test")

    assert temp_store.search_facts("\x00") == []
    assert [r["content"] for r in temp_store.search_facts("cat\x00")] == [
        "the cat sat on the mat"
    ]
    assert [r["content"] for r in temp_store.search_facts("cat\x00mat")] == [
        "the cat sat on the mat"
    ]


@pytest.mark.parametrize("query", ["", "   ", "\x00", "\x00\x00"])
def test_search_facts_empty_query_short_circuits(temp_store, query):
    temp_store.add_fact("the cat sat on the mat", category="test")

    assert temp_store.search_facts(query) == []


def test_search_facts_no_hits_does_not_bump_retrieval_count(temp_store):
    fact_id = temp_store.add_fact("the cat sat on the mat", category="test")

    assert temp_store.search_facts("col:val") == []

    stored = temp_store.list_facts()[0]
    assert stored["fact_id"] == fact_id
    assert stored["retrieval_count"] == 0


@pytest.mark.parametrize(
    "query",
    [
        "col:val",
        'foo"bar',
        '"unclosed',
        "NEAR/",
        "*",
        "***",
        '" "',
    ],
)
def test_fact_retriever_fts_candidates_no_hits_for_fts5_syntax_text(
    temp_store,
    query,
):
    temp_store.add_fact("the cat sat on the mat", category="test")
    retriever = FactRetriever(temp_store, hrr_weight=0.0)

    assert retriever._fts_candidates(query, None, 0.3, 10) == []
    assert retriever.search(query) == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("cat:", "the cat sat on the mat"),
        ("issue:123", "see issue:123 for details"),
        ("http://example.com", "visit http://example.com now"),
        ("user's manual", "the user's manual"),
        # "parser AND", not "a AND": the RETRIEVER (unlike search_facts above)
        # compiles OR queries with stopwords filtered, so a query made only of
        # stopwords now compiles to nothing -- deliberately, because OR-ing
        # common English words floods the candidate set before the reranker
        # sees a relevant row. The property under test is unchanged: AND must
        # be a literal token, not an FTS5 operator.
        ("parser AND", "parser AND indexer are both listed"),
    ],
)
def test_fact_retriever_fts_candidates_treats_query_as_literal_text(
    temp_store,
    query,
    expected,
):
    for content in (
        "the cat sat on the mat",
        "see issue:123 for details",
        "visit http://example.com now",
        "the user's manual",
        "parser AND indexer are both listed",
    ):
        temp_store.add_fact(content, category="test")
    retriever = FactRetriever(
        temp_store,
        fts_weight=1.0,
        jaccard_weight=0.0,
        hrr_weight=0.0,
    )

    candidates = retriever._fts_candidates(query, "test", 0.3, 10)
    assert [result["content"] for result in candidates] == [expected]
    assert candidates[0]["fts_rank"] == pytest.approx(1.0)

    results = retriever.search(query, category="test")
    assert [result["content"] for result in results] == [expected]
    assert results[0]["score"] == pytest.approx(0.5)


@pytest.mark.parametrize("query", ["", "   ", "\x00", "\x00\x00"])
def test_fact_retriever_fts_candidates_empty_query_short_circuits(
    temp_store,
    query,
):
    temp_store.add_fact("the cat sat on the mat", category="test")
    temp_store._conn.execute("DROP TABLE facts_fts")
    retriever = FactRetriever(temp_store, hrr_weight=0.0)

    assert retriever._fts_candidates(query, None, 0.3, 10) == []
    assert retriever.search(query) == []


def test_fact_retriever_fts_candidates_propagates_real_database_errors(temp_store):
    temp_store.add_fact("the cat sat on the mat", category="test")
    temp_store._conn.execute("DROP TABLE facts_fts")

    with pytest.raises(sqlite3.OperationalError, match="facts_fts"):
        FactRetriever(temp_store)._fts_candidates("cat", None, 0.3, 10)


@pytest.mark.parametrize("hrr_dim", [0, -1, -1024])
def test_memory_store_rejects_non_positive_hrr_dim(tmp_path, hrr_dim):
    """Reject before touching the DB, so no partial fact row can be committed."""
    db_path = tmp_path / "bad_dim.db"

    with pytest.raises(ValueError, match="hrr_dim must be positive"):
        MemoryStore(db_path=db_path, hrr_dim=hrr_dim)

    assert not db_path.exists()


def test_search_facts_propagates_real_database_errors(temp_store):
    """Infrastructure failures must not be masked as "no results"."""
    temp_store.add_fact("the cat sat on the mat", category="test")
    temp_store._conn.execute("DROP TABLE facts_fts")

    with pytest.raises(sqlite3.OperationalError, match="facts_fts"):
        temp_store.search_facts("cat")


def test_fact_retriever_hybrid_search_uses_fts_jaccard_and_hrr(tmp_path):
    store = MemoryStore(db_path=tmp_path / "hybrid.db", hrr_dim=128)
    try:
        exact_id = store.add_fact("cache eviction", category="jaccard")
        noisy_id = store.add_fact(
            "cache eviction unrelated banana satellite calculus",
            category="jaccard",
        )
        jaccard_results = FactRetriever(
            store, fts_weight=0.0, jaccard_weight=1.0, hrr_weight=0.0
        ).search("cache eviction", category="jaccard", limit=2)
        assert [result["fact_id"] for result in jaccard_results] == [
            exact_id,
            noisy_id,
        ]
        assert jaccard_results[0]["score"] > jaccard_results[1]["score"]

        repeated_id = store.add_fact(
            "cache eviction cache eviction",
            category="fts",
        )
        plain_id = store.add_fact("cache eviction ordinary", category="fts")
        fts_results = FactRetriever(
            store, fts_weight=1.0, jaccard_weight=0.0, hrr_weight=0.0
        ).search("cache eviction", category="fts", limit=2)
        assert [result["fact_id"] for result in fts_results] == [
            repeated_id,
            plain_id,
        ]
        assert fts_results[0]["fts_rank"] > fts_results[1]["fts_rank"]

        if not hrr.HAS_NUMPY:
            pytest.skip("NumPy is required for HRR similarity assertions")

        matching_id = store.add_fact("vector probe cache", category="hrr")
        distant_id = store.add_fact("vector probe unrelated", category="hrr")
        query_vec = hrr.encode_text("vector probe", store.hrr_dim)
        distant_vec = hrr.encode_text("orthogonal archive", store.hrr_dim)
        store._conn.execute(
            "UPDATE facts SET hrr_vector = ? WHERE fact_id = ?",
            (hrr.phases_to_bytes(query_vec), matching_id),
        )
        store._conn.execute(
            "UPDATE facts SET hrr_vector = ? WHERE fact_id = ?",
            (hrr.phases_to_bytes(distant_vec), distant_id),
        )
        store._conn.commit()

        hrr_results = FactRetriever(
            store, fts_weight=0.0, jaccard_weight=0.0, hrr_weight=1.0
        ).search("vector probe", category="hrr", limit=2)
        assert [result["fact_id"] for result in hrr_results] == [
            matching_id,
            distant_id,
        ]
        assert hrr_results[0]["score"] == pytest.approx(0.5)
        assert hrr_results[0]["score"] > hrr_results[1]["score"]
    finally:
        store.close()


def test_fact_retriever_probe_returns_single_entity_matches(temp_store):
    first_id = temp_store.add_fact(
        'Ada Lovelace designed notes for the "Analytical Engine".',
        category="history",
    )
    second_id = temp_store.add_fact(
        'Ada Lovelace corresponded with "Charles Babbage".',
        category="history",
    )
    unrelated_id = temp_store.add_fact(
        'Grace Hopper popularized "COBOL".',
        category="history",
    )

    results = FactRetriever(temp_store).probe(
        "Ada Lovelace",
        category="history",
        limit=2,
    )

    assert [result["fact_id"] for result in results] == [first_id, second_id]
    assert unrelated_id not in {result["fact_id"] for result in results}
    assert all(result["score"] == pytest.approx(0.5) for result in results)
    assert _fact_by_id(temp_store, first_id)["retrieval_count"] == 1
    assert _fact_by_id(temp_store, second_id)["retrieval_count"] == 1
    assert _fact_by_id(temp_store, unrelated_id)["retrieval_count"] == 0


def test_fact_retriever_reason_returns_multi_entity_joint_matches(temp_store):
    joint_id = temp_store.add_fact(
        'Ada Lovelace documented the "Analytical Engine".',
        category="history",
    )
    ada_only_id = temp_store.add_fact(
        'Ada Lovelace corresponded with "Charles Babbage".',
        category="history",
    )
    engine_only_id = temp_store.add_fact(
        'Charles Babbage proposed the "Analytical Engine".',
        category="history",
    )

    results = FactRetriever(temp_store).reason(
        ["Ada Lovelace", "Analytical Engine"],
        category="history",
        limit=3,
    )

    assert [result["fact_id"] for result in results] == [joint_id]
    assert results[0]["score"] == pytest.approx(0.5)
    assert _fact_by_id(temp_store, joint_id)["retrieval_count"] == 1
    assert _fact_by_id(temp_store, ada_only_id)["retrieval_count"] == 0
    assert _fact_by_id(temp_store, engine_only_id)["retrieval_count"] == 0


def test_hrr_unbind_role_entity_recovers_bundled_entities():
    if not hrr.HAS_NUMPY:
        pytest.skip("NumPy is required for HRR similarity assertions")

    dim = 1024
    role_entity = hrr.encode_atom("__hrr_role_entity__", dim)
    fact_vec = hrr.encode_fact(
        "lowercase analytical engine memo",
        ["Ada Lovelace", "Analytical Engine"],
        dim,
    )

    recovered = hrr.unbind(fact_vec, role_entity)
    ada_sim = hrr.similarity(recovered, hrr.encode_atom("ada lovelace", dim))
    engine_sim = hrr.similarity(recovered, hrr.encode_atom("analytical engine", dim))
    unrelated_sim = hrr.similarity(recovered, hrr.encode_atom("grace hopper", dim))

    assert ada_sim > unrelated_sim + 0.20
    assert engine_sim > unrelated_sim + 0.20
    assert max(ada_sim, engine_sim) > 0.30
    assert abs(unrelated_sim) < 0.15


def test_fact_retriever_probe_hrr_similarity_ranking(temp_store):
    if not hrr.HAS_NUMPY:
        pytest.skip("NumPy is required for HRR retrieval assertions")

    target_content = "lowercase vector target memo"
    unrelated_content = "lowercase vector unrelated memo"
    target_id = temp_store.add_fact(target_content, category="hrr_probe_rank")
    unrelated_id = temp_store.add_fact(unrelated_content, category="hrr_probe_rank")
    _set_fact_hrr(temp_store, target_id, target_content, ["Ada Lovelace"])
    _set_fact_hrr(temp_store, unrelated_id, unrelated_content, ["Grace Hopper"])

    results = FactRetriever(temp_store).probe(
        "Ada Lovelace",
        category="hrr_probe_rank",
        limit=2,
    )

    assert [result["fact_id"] for result in results] == [target_id, unrelated_id]
    assert results[0]["score"] > results[1]["score"] + 0.05


def test_fact_retriever_reason_hrr_similarity_ranking(temp_store):
    if not hrr.HAS_NUMPY:
        pytest.skip("NumPy is required for HRR retrieval assertions")

    targets = ["Ada Lovelace", "Analytical Engine"]
    role_entity = hrr.encode_atom("__hrr_role_entity__", temp_store.hrr_dim)
    target_vecs = [hrr.encode_atom(target.lower(), temp_store.hrr_dim) for target in targets]

    partial_vec = hrr.encode_fact("lowercase partial vector memo", [targets[0]], temp_store.hrr_dim)
    partial_recovered = hrr.unbind(partial_vec, role_entity)
    partial_min = min(hrr.similarity(partial_recovered, tv) for tv in target_vecs)
    unrelated_entity = None
    for candidate in ("Grace Hopper", "COBOL", "FORTRAN", "Compiler Design"):
        candidate_vec = hrr.encode_fact("lowercase unrelated vector memo", [candidate], temp_store.hrr_dim)
        candidate_recovered = hrr.unbind(candidate_vec, role_entity)
        candidate_min = min(hrr.similarity(candidate_recovered, tv) for tv in target_vecs)
        if partial_min > candidate_min + 0.02:
            unrelated_entity = candidate
            break
    assert unrelated_entity is not None

    joint_content = "lowercase joint vector memo"
    partial_content = "lowercase partial vector memo"
    unrelated_content = "lowercase unrelated vector memo"
    joint_id = temp_store.add_fact(joint_content, category="hrr_reason_rank")
    partial_id = temp_store.add_fact(partial_content, category="hrr_reason_rank")
    unrelated_id = temp_store.add_fact(unrelated_content, category="hrr_reason_rank")
    _set_fact_hrr(temp_store, joint_id, joint_content, targets)
    _set_fact_hrr(temp_store, partial_id, partial_content, [targets[0]])
    _set_fact_hrr(temp_store, unrelated_id, unrelated_content, [unrelated_entity])

    results = FactRetriever(temp_store).reason(
        targets,
        category="hrr_reason_rank",
        limit=3,
    )

    assert [result["fact_id"] for result in results] == [
        joint_id,
        partial_id,
        unrelated_id,
    ]
    assert results[0]["score"] > results[1]["score"] + 0.05


def test_fact_retriever_probe_and_reason_count_hrr_scored_results(temp_store):
    if not hrr.HAS_NUMPY:
        pytest.skip("NumPy is required for HRR retrieval assertions")

    probe_ids = [
        temp_store.add_fact("alpha vector retrieval target", category="hrr_probe"),
        temp_store.add_fact("beta vector retrieval alternate", category="hrr_probe"),
    ]
    probe_results = FactRetriever(temp_store).probe(
        "unlinked probe entity",
        category="hrr_probe",
        limit=1,
    )

    assert len(probe_results) == 1
    assert _fact_by_id(temp_store, probe_results[0]["fact_id"])["retrieval_count"] == 1
    assert [
        _fact_by_id(temp_store, fact_id)["retrieval_count"]
        for fact_id in probe_ids
        if fact_id != probe_results[0]["fact_id"]
    ] == [0]

    reason_ids = [
        temp_store.add_fact("gamma vector reasoning target", category="hrr_reason"),
        temp_store.add_fact("delta vector reasoning alternate", category="hrr_reason"),
        temp_store.add_fact("epsilon vector reasoning reserve", category="hrr_reason"),
    ]
    reason_results = FactRetriever(temp_store).reason(
        ["unlinked reason one", "unlinked reason two"],
        category="hrr_reason",
        limit=2,
    )

    returned_reason_ids = {result["fact_id"] for result in reason_results}
    assert len(reason_results) == 2
    assert [
        _fact_by_id(temp_store, fact_id)["retrieval_count"]
        for fact_id in reason_ids
        if fact_id in returned_reason_ids
    ] == [1, 1]
    assert [
        _fact_by_id(temp_store, fact_id)["retrieval_count"]
        for fact_id in reason_ids
        if fact_id not in returned_reason_ids
    ] == [0]


def test_fact_retriever_contradict_detects_shared_entity_divergence(temp_store):
    if not hrr.HAS_NUMPY:
        pytest.skip("NumPy is required for HRR contradiction detection")

    green_id = temp_store.add_fact(
        'Project Atlas deployment status is "green".',
        category="ops",
    )
    red_id = temp_store.add_fact(
        'Project Atlas deployment status is "red".',
        category="ops",
    )
    unrelated_id = temp_store.add_fact(
        'Project Borealis deployment status is "stable".',
        category="ops",
    )

    contradictions = FactRetriever(temp_store).contradict(
        category="ops",
        threshold=0.05,
        limit=5,
    )
    matching = [
        item for item in contradictions
        if {item["fact_a"]["fact_id"], item["fact_b"]["fact_id"]} == {green_id, red_id}
    ]

    assert matching
    assert matching[0]["shared_entities"] == ["project atlas"]
    assert matching[0]["contradiction_score"] >= 0.05
    assert all(
        unrelated_id not in {item["fact_a"]["fact_id"], item["fact_b"]["fact_id"]}
        for item in contradictions
    )


def test_run_hygiene_resolves_contradiction_penalizes_lower_trust(temp_store):
    manager = IndexManager(storage=temp_store)
    low_id = temp_store.add_fact('Project Atlas deployment status is "red".')
    high_id = temp_store.add_fact('Project Atlas deployment status is "green".')
    _set_fact_state(temp_store, low_id, trust_score=0.30)
    _set_fact_state(temp_store, high_id, trust_score=0.70)
    manager._retriever.contradict = lambda: [
        {
            "fact_a": {"fact_id": high_id, "trust_score": 0.10},
            "fact_b": {"fact_id": low_id, "trust_score": 0.90},
        }
    ]

    assert manager.run_hygiene() == 0

    assert _fact_by_id(temp_store, low_id)["trust_score"] == pytest.approx(0.20)
    assert _fact_by_id(temp_store, high_id)["trust_score"] == pytest.approx(0.70)


def test_run_hygiene_resolves_contradiction_penalizes_older_equal_trust_fact(temp_store):
    manager = IndexManager(storage=temp_store)
    old_id = temp_store.add_fact('Project Atlas deployment status is "red".')
    new_id = temp_store.add_fact('Project Atlas deployment status is "green".')
    _set_fact_state(
        temp_store, old_id, trust_score=0.50, created_at="2024-01-01 00:00:00"
    )
    _set_fact_state(
        temp_store, new_id, trust_score=0.50, created_at="2024-01-02 00:00:00"
    )
    manager._retriever.contradict = lambda: [
        {"fact_a": {"fact_id": new_id}, "fact_b": {"fact_id": old_id}}
    ]

    assert manager.run_hygiene() == 0

    assert _fact_by_id(temp_store, old_id)["trust_score"] == pytest.approx(0.40)
    assert _fact_by_id(temp_store, new_id)["trust_score"] == pytest.approx(0.50)


def test_run_hygiene_skips_contradictions_with_removed_facts(temp_store):
    manager = IndexManager(storage=temp_store)
    removed_id = temp_store.add_fact('Project Atlas deployment status is "red".')
    survivor_id = temp_store.add_fact('Project Atlas deployment status is "green".')
    temp_store.remove_fact(removed_id)
    manager._retriever.contradict = lambda: [
        {"fact_a": {"fact_id": removed_id}, "fact_b": {"fact_id": survivor_id}}
    ]

    assert manager.run_hygiene() == 0
    assert _fact_by_id(temp_store, survivor_id)["trust_score"] == pytest.approx(0.50)


def test_run_hygiene_prunes_contradictory_fact_below_threshold(temp_store):
    manager = IndexManager(storage=temp_store)
    loser_id = temp_store.add_fact('Project Atlas deployment status is "red".')
    winner_id = temp_store.add_fact('Project Atlas deployment status is "green".')
    _set_fact_state(temp_store, loser_id, trust_score=0.10, retrieval_count=1)
    _set_fact_state(temp_store, winner_id, trust_score=0.60)
    manager._retriever.contradict = lambda: [
        {"fact_a": {"fact_id": loser_id}, "fact_b": {"fact_id": winner_id}}
    ]

    assert manager.run_hygiene() == 1

    assert _fact_by_id(temp_store, loser_id) is None
    assert _fact_by_id(temp_store, winner_id)["trust_score"] == pytest.approx(0.60)


def test_run_hygiene_spares_low_trust_facts_that_proved_useful(temp_store):
    """A reprieve is earned by being useful, not by being surfaced.

    This guard used to key on retrieval_count, back when nothing on the
    improvement cycle's path incremented it -- so "never retrieved" was every
    fact and the clause never fired. Now that retrieval counts, keying on it
    would invert the intent: a discredited fact retrieved once would become
    permanently unprunable, and the more often a bad fact got surfaced the
    safer it would be. helpful_count records the thing that should actually
    buy protection.
    """
    manager = IndexManager(storage=temp_store)
    never_useful_id = temp_store.add_fact("discarded low trust fact")
    was_useful_id = temp_store.add_fact("retained low trust fact that once helped")
    normal_id = temp_store.add_fact("retained ordinary fact")
    _set_fact_state(temp_store, never_useful_id, trust_score=0.05, retrieval_count=9)
    _set_fact_state(temp_store, was_useful_id, trust_score=0.05, retrieval_count=0)
    temp_store._conn.execute(
        "UPDATE facts SET helpful_count = 1 WHERE fact_id = ?", (was_useful_id,)
    )
    temp_store._conn.commit()
    manager._retriever.contradict = lambda: []

    assert manager.run_hygiene() == 1

    # Retrieved nine times and never once helpful -- being surfaced is not a defence.
    assert _fact_by_id(temp_store, never_useful_id) is None
    assert _fact_by_id(temp_store, was_useful_id)["trust_score"] == pytest.approx(0.05)
    assert _fact_by_id(temp_store, normal_id)["trust_score"] == pytest.approx(0.50)


def test_run_hygiene_prunes_to_snr(tmp_path):
    store = MemoryStore(db_path=tmp_path / "hygiene_snr.db", hrr_dim=16)
    try:
        manager = IndexManager(storage=store)
        manager._retriever.contradict = lambda: []
        for i in range(6):
            store.add_fact(f"hygiene snr fact {i}", category="hygiene")

        assert manager.run_hygiene() == 2

        count = store._conn.execute(
            "SELECT COUNT(*) FROM facts WHERE category = ?", ("hygiene",)
        ).fetchone()[0]
        assert count == 4
    finally:
        store.close()


def test_run_hygiene_calls_contradict_with_no_numpy(temp_store, monkeypatch):
    manager = IndexManager(storage=temp_store)
    fact_id = temp_store.add_fact("Project Atlas deployment status is stable.")
    original_contradict = FactRetriever.contradict
    called = False

    def spy_contradict(self):
        nonlocal called
        called = True
        return original_contradict(self)

    monkeypatch.setattr(FactRetriever, "contradict", spy_contradict)
    monkeypatch.setattr(hrr, "HAS_NUMPY", False)

    assert manager.run_hygiene() == 0
    assert called
    assert _fact_by_id(temp_store, fact_id) is not None
