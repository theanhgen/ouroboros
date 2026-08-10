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

    assert {fact["fact_id"] for fact in facts} == set(fact_ids)
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


def test_fact_retriever_reason_returns_multi_entity_joint_matches(temp_store):
    joint_id = temp_store.add_fact(
        'Ada Lovelace documented the "Analytical Engine".',
        category="history",
    )
    temp_store.add_fact(
        'Ada Lovelace corresponded with "Charles Babbage".',
        category="history",
    )
    temp_store.add_fact(
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
