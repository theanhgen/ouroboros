"""Tests for codebase self-reader."""

import ast
import textwrap
from pathlib import Path

import ouroboros
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


def test_get_function_signatures_attributes_nested_class(tmp_path):
    """A method belongs to its immediate class, not the outermost one."""
    test_file = tmp_path / 'nested.py'
    test_file.write_text(textwrap.dedent('''
        class Outer:
            class Inner:
                def m(self):
                    pass
    ''').lstrip())

    assert get_function_signatures(test_file) == [
        {'name': 'm', 'args': ['self'], 'line': 3, 'type': 'method', 'class': 'Inner'},
    ]


def test_get_function_signatures_nested_function_is_not_a_method(tmp_path):
    """Only a direct child of a class body is a method."""
    test_file = tmp_path / 'inner.py'
    test_file.write_text(textwrap.dedent('''
        class C:
            def m(self):
                def helper():
                    pass
                return helper
    ''').lstrip())

    assert get_function_signatures(test_file) == [
        {'name': 'm', 'args': ['self'], 'line': 2, 'type': 'method', 'class': 'C'},
        {'name': 'helper', 'args': [], 'line': 3, 'type': 'function'},
    ]


def test_get_function_signatures_indirect_class_child_is_not_a_method(tmp_path):
    """A def under an `if` in a class body is not a direct child of the class."""
    test_file = tmp_path / 'guarded.py'
    test_file.write_text(textwrap.dedent('''
        import typing

        class C:
            if typing.TYPE_CHECKING:
                def m(self):
                    pass
    ''').lstrip())

    assert get_function_signatures(test_file) == [
        {'name': 'm', 'args': ['self'], 'line': 5, 'type': 'function'},
    ]


def test_get_function_signatures_async_method(tmp_path):
    test_file = tmp_path / 'async_mod.py'
    test_file.write_text(textwrap.dedent('''
        class C:
            async def fetch(self, url):
                pass
    ''').lstrip())

    assert get_function_signatures(test_file) == [
        {'name': 'fetch', 'args': ['self', 'url'], 'line': 2,
         'type': 'method', 'class': 'C'},
    ]


def test_get_function_signatures_all_parameter_types(tmp_path):
    test_file = tmp_path / 'params.py'
    test_file.write_text(textwrap.dedent('''
        def complex_func(pos_only, /, regular, *args, kw_only, **kwargs):
            pass

        def keyword_only_func(*, key_only=True, **kwargs):
            pass

        class Service:
            def sync_method(self, raw, /, regular, *args, kw_only, **kwargs):
                pass

            async def async_method(self, first, /, second, *items, flag=False, **options):
                pass
    ''').lstrip())

    assert get_function_signatures(test_file) == [
        {
            'name': 'complex_func',
            'args': ['pos_only', 'regular', '*args', 'kw_only', '**kwargs'],
            'line': 1,
            'type': 'function',
        },
        {
            'name': 'keyword_only_func',
            'args': ['key_only', '**kwargs'],
            'line': 4,
            'type': 'function',
        },
        {
            'name': 'sync_method',
            'args': ['self', 'raw', 'regular', '*args', 'kw_only', '**kwargs'],
            'line': 8,
            'type': 'method',
            'class': 'Service',
        },
        {
            'name': 'async_method',
            'args': ['self', 'first', 'second', '*items', 'flag', '**options'],
            'line': 11,
            'type': 'method',
            'class': 'Service',
        },
    ]


def test_get_function_signatures_preserves_breadth_first_order(tmp_path):
    """Shallower definitions are reported before deeper ones."""
    test_file = tmp_path / 'order.py'
    test_file.write_text(textwrap.dedent('''
        def first():
            pass

        class C:
            def second(self):
                def third():
                    pass
    ''').lstrip())

    assert [s['name'] for s in get_function_signatures(test_file)] == [
        'first', 'second', 'third',
    ]


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


def test_extract_code_metadata_all_parameter_types():
    code = textwrap.dedent('''
        def complex_func(pos_only, /, regular, *args, kw_only, **kwargs):
            pass

        def keyword_only_func(*, key_only=True, **kwargs):
            pass

        class Service:
            def sync_method(self, raw, /, regular, *args, kw_only, **kwargs):
                pass

            async def async_method(self, first, /, second, *items, flag=False, **options):
                pass
    ''').lstrip()

    metadata = extract_code_metadata(code, "params.py")

    assert [func.name for func in metadata.functions] == [
        "complex_func", "keyword_only_func",
    ]
    assert metadata.functions[0].args == [
        "pos_only", "regular", "*args", "kw_only", "**kwargs",
    ]
    assert metadata.functions[1].args == ["key_only", "**kwargs"]

    assert len(metadata.classes) == 1
    service = metadata.classes[0]
    assert [method.name for method in service.methods] == [
        "sync_method", "async_method",
    ]
    assert service.methods[0].args == [
        "self", "raw", "regular", "*args", "kw_only", "**kwargs",
    ]
    assert service.methods[1].args == [
        "self", "first", "second", "*items", "flag", "**options",
    ]


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


# Modules reached from outside the package rather than by an import from a
# sibling: __init__ is the package itself, __main__ backs `python -m ouroboros`.
# cli is deliberately absent -- __main__ imports it.
PACKAGE_ENTRYPOINTS = frozenset({"__init__", "__main__"})


def _package_local_imports(tree: ast.Module) -> set[str]:
    """Top-level ouroboros submodule names imported by one parsed module."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and not node.module:
                # from . import backlog, llm
                found.update(alias.name for alias in node.names)
            elif node.level:
                # from .moltbook import load_runner_config
                found.add(node.module.split(".")[0])
            elif node.module and node.module.split(".")[0] == "ouroboros":
                # from ouroboros.moltbook import load_runner_config
                parts = node.module.split(".")
                if len(parts) > 1:
                    found.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "ouroboros" and len(parts) > 1:
                    found.add(parts[1])
    return found


def test_every_package_module_has_an_importer():
    """No module under src/ouroboros/ may sit with no importer in the package.

    self_improve.py shipped that way and stayed: nothing in src/ ever imported
    it in any commit, so run_self_improve never ran, while its six unit tests
    kept passing and reported the capability as covered (#108, #109). The agent
    chooses its daily work by reading this same tree, so an unreachable module
    is not inert -- it is a live candidate to spend an improvement on.

    A module genuinely entered from outside the package belongs in
    PACKAGE_ENTRYPOINTS, with the reason recorded there.
    """
    pkg_dir = Path(ouroboros.__file__).resolve().parent

    modules = {p.stem for p in pkg_dir.glob("*.py")} - PACKAGE_ENTRYPOINTS
    imported: set[str] = set()
    for path in sorted(pkg_dir.glob("*.py")):
        imported |= _package_local_imports(
            ast.parse(path.read_text(encoding="utf-8"))
        )

    orphans = sorted(modules - imported)
    assert orphans == [], (
        f"No module in src/ouroboros/ imports: {orphans}. "
        "Wire each one in, delete it, or add it to PACKAGE_ENTRYPOINTS."
    )
