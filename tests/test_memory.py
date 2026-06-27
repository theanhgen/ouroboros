"""Tests for holographic memory and AST-based code indexing."""

import tempfile
from pathlib import Path
import pytest
import textwrap

from ouroboros.memory import IndexManager, MemoryStore


@pytest.fixture
def temp_store(tmp_path):
    db_path = tmp_path / "test_memory.db"
    store = MemoryStore(db_path=db_path)
    yield store
    store.close()


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
