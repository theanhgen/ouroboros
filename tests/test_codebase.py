"""Tests for codebase self-reader."""

import textwrap
from pathlib import Path

from ouroboros.codebase import (
    get_function_signatures,
    get_repo_root,
    list_source_files,
    get_test_files,
    read_file,
    read_file_raw,
    get_codebase_summary,
)


def test_get_repo_root():
    root = get_repo_root()
    assert root.exists()
    assert (root / ".git").exists()


def test_list_source_files():
    files = list_source_files()
    assert len(files) > 0
    assert all(f.suffix == ".py" for f in files)
    # Should contain our new files
    names = [f.name for f in files]
    assert "codebase.py" in names
    assert "config.py" in names


def test_get_test_files():
    files = get_test_files()
    assert len(files) > 0
    assert all(f.suffix == ".py" for f in files)


def test_read_file():
    root = get_repo_root()
    config_path = root / "src" / "ouroboros" / "config.py"
    content = read_file(config_path)
    assert "SafetyConfig" in content
    # Should have line numbers
    assert "   1 |" in content


def test_read_file_raw():
    root = get_repo_root()
    config_path = root / "src" / "ouroboros" / "config.py"
    content = read_file_raw(config_path)
    assert "SafetyConfig" in content
    # Should NOT have line numbers
    assert "   1 |" not in content


def test_read_file_not_found():
    try:
        read_file(Path("/nonexistent/file.py"))
        assert False, "Should have raised FileNotFoundError"
    except FileNotFoundError:
        pass


def test_get_function_signatures(tmp_path):
    code = textwrap.dedent("""
        def hello(name: str) -> str:
            return f"hello {name}"

        class MyClass:
            def method(self, x, y):
                return x + y
    """)
    test_file = tmp_path / "test_module.py"
    test_file.write_text(code)

    sigs = get_function_signatures(test_file)
    assert len(sigs) >= 2

    hello_sig = next(s for s in sigs if s["name"] == "hello")
    assert hello_sig["args"] == ["name"]
    assert hello_sig["type"] == "function"
    assert hello_sig["line"] == 1

    method_sig = next(s for s in sigs if s["name"] == "method")
    assert method_sig["args"] == ["self", "x", "y"]


def test_get_function_signatures_syntax_error(tmp_path):
    bad_file = tmp_path / "bad.py"
    bad_file.write_text("def incomplete(")
    sigs = get_function_signatures(bad_file)
    assert sigs == []


def test_get_codebase_summary():
    summary = get_codebase_summary()
    assert "# Codebase Summary" in summary
    assert "Source Files" in summary
    assert "Test Files" in summary
    assert "config.py" in summary


def test_list_source_files_nonexistent(tmp_path):
    files = list_source_files(tmp_path)
    assert files == []


def test_get_test_files_nonexistent(tmp_path):
    files = get_test_files(tmp_path)
    assert files == []
