"""Tests for codebase self-reader."""

import textwrap
from pathlib import Path

from ouroboros.codebase import (
    extract_code_metadata,
    get_function_signatures,
    get_repo_root,
    list_source_files,
    get_test_files,
    read_file,
    read_file_raw,
    get_codebase_summary,
)


# Ensure correct path handling and root identifying

def test_get_repo_root():
    root = get_repo_root()
    assert root.exists(), "The repository root does not exist."
    assert (root / '.git').exists(), "The '.git' directory is missing at the repo root."


def test_list_source_files():
    files = list_source_files()
    assert len(files) > 0, "No source files found."
    assert all(f.suffix == '.py' for f in files), "Not all listed files are Python files."
    names = [f.name for f in files]
    assert 'codebase.py' in names, "File 'codebase.py' is missing."


def test_get_test_files():
    files = get_test_files()
    assert len(files) > 0, "No test files found."
    assert all(f.suffix == '.py' for f in files), "Not all listed files are Python files."


def test_read_file():
    root = get_repo_root()
    codebase_path = root / 'src' / 'ouroboros' / 'codebase.py'
    content = read_file(codebase_path)
    assert 'extract_code_metadata' in content, "Content doesn't match expectations, 'extract_code_metadata' not found."
    assert '   1 |' in content, "Line numbers are missing in file content."


def test_read_file_raw():
    root = get_repo_root()
    codebase_path = root / 'src' / 'ouroboros' / 'codebase.py'
    content = read_file_raw(codebase_path)
    assert 'extract_code_metadata' in content, "Content doesn't match expectations, 'extract_code_metadata' not found."
    assert '   1 |' not in content, "Line numbers should not be included in raw file reading."


def test_read_file_not_found():
    try:
        read_file(Path('/nonexistent/file.py'))
        assert False, "Should have raised FileNotFoundError but did not."
    except FileNotFoundError:
        pass


def test_get_function_signatures(tmp_path):
    code = textwrap.dedent('''
        def hello(name: str) -> str:
            return f"hello {name}"

        class MyClass:
            def method(self, x, y):
                return x + y
    ''').lstrip()
    test_file = tmp_path / 'test_module.py'
    test_file.write_text(code)

    sigs = get_function_signatures(test_file)
    assert len(sigs) >= 2

    hello_sig = next(s for s in sigs if s['name'] == 'hello')
    assert hello_sig['args'] == ['name']
    assert hello_sig['type'] == 'function'
    assert hello_sig['line'] == 1

    method_sig = next(s for s in sigs if s['name'] == 'method')
    assert method_sig['args'] == ['self', 'x', 'y']
    assert method_sig['line'] == 5


def test_get_function_signatures_syntax_error(tmp_path):
    bad_file = tmp_path / 'bad.py'
    bad_file.write_text('def incomplete(')
    sigs = get_function_signatures(bad_file)
    assert sigs == [], "Syntax error should result in no signatures being returned."


def test_get_codebase_summary():
    summary = get_codebase_summary()
    assert '# Codebase Summary' in summary
    assert 'Source Files' in summary
    assert 'Test Files' in summary
    assert 'codebase.py' in summary


def test_extract_code_metadata_async_functions_and_methods():
    code = textwrap.dedent('''
        async def fetch_value(client):
            """Fetch a value asynchronously."""
            return await client.fetch()

        class Service:
            async def refresh(self, key):
                """Refresh one key."""
                return key

            def sync_method(self):
                return None
    ''').lstrip()

    metadata = extract_code_metadata(code, "service.py")

    assert [func.name for func in metadata.functions] == ["fetch_value"]
    assert metadata.functions[0].args == ["client"]
    assert metadata.functions[0].docstring == "Fetch a value asynchronously."

    assert len(metadata.classes) == 1
    service = metadata.classes[0]
    assert service.name == "Service"
    assert [method.name for method in service.methods] == ["refresh", "sync_method"]

    refresh = service.methods[0]
    assert refresh.args == ["self", "key"]
    assert refresh.docstring == "Refresh one key."


def test_get_codebase_summary_includes_async_metadata(tmp_path):
    src_dir = tmp_path / 'src' / 'ouroboros'
    tests_dir = tmp_path / 'tests'
    src_dir.mkdir(parents=True)
    tests_dir.mkdir()
    (src_dir / 'async_module.py').write_text(textwrap.dedent('''
        async def fetch_value(client):
            return await client.fetch()

        class Service:
            async def refresh(self, key):
                return key
    ''').lstrip())

    summary = get_codebase_summary(tmp_path)

    assert 'async_module.py' in summary
    assert 'def fetch_value(client)' in summary
    assert 'def refresh(self, key)' in summary


def test_list_source_files_nonexistent(tmp_path):
    files = list_source_files(tmp_path)
    assert files == [], "Expected no files but got some unexpectedly."


def test_get_test_files_nonexistent(tmp_path):
    files = get_test_files(tmp_path)
    assert files == [], "Expected no test files but got some unexpectedly."
