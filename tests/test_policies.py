"""Tests for policies module."""

import pytest

from ouroboros.config import SafetyConfig
from ouroboros.policies import (
    PolicyError,
    require_pr_only,
    validate_change_size,
    validate_import_policy,
    validate_modification_scope,
)


def test_require_pr_only_passes():
    require_pr_only(True)  # should not raise


def test_require_pr_only_fails():
    try:
        require_pr_only(False)
        assert False, "Should have raised PolicyError"
    except PolicyError:
        pass


def test_validate_modification_scope_allowed():
    violations = validate_modification_scope(["src/ouroboros/llm.py", "tests/test_foo.py"])
    assert violations == []


def test_validate_modification_scope_forbidden():
    violations = validate_modification_scope(["src/ouroboros/config.py"])
    assert len(violations) == 1
    assert "Forbidden" in violations[0]


def test_validate_modification_scope_forbidden_path_and_prefix():
    config = SafetyConfig(
        forbidden_modification_paths=(
            "src/ouroboros/secret.py",
            "src/ouroboros/forbidden_dir/",
        )
    )

    violations = validate_modification_scope(["src/ouroboros/secret.py"], config)
    assert len(violations) == 1
    assert "Forbidden" in violations[0]

    violations = validate_modification_scope(
        ["src/ouroboros/forbidden_dir/nested.py"], config
    )
    assert len(violations) == 1
    assert "Forbidden" in violations[0]

    violations = validate_modification_scope(
        ["src/ouroboros/forbidden_dir_not/nested.py"], config
    )
    assert violations == []


def test_validate_modification_scope_out_of_scope():
    violations = validate_modification_scope(["README.md"])
    assert len(violations) == 1
    assert "Out of scope" in violations[0]


def test_validate_modification_scope_mixed():
    violations = validate_modification_scope([
        "src/ouroboros/llm.py",  # ok
        "src/ouroboros/config.py",  # forbidden
        "setup.py",  # out of scope
    ])
    assert len(violations) == 2


def test_validate_change_size_ok():
    violations = validate_change_size(2, 100)
    assert violations == []


def test_validate_change_size_too_many_files():
    violations = validate_change_size(5, 100)
    assert len(violations) == 1
    assert "files" in violations[0].lower()


def test_validate_change_size_too_many_lines():
    violations = validate_change_size(1, 500)
    assert len(violations) == 1
    assert "lines" in violations[0].lower()


def test_validate_change_size_both_exceeded():
    violations = validate_change_size(5, 500)
    assert len(violations) == 2


def test_config_defaults_are_safe():
    config = SafetyConfig()
    assert config.pr_only is True
    assert config.allow_write_default_branch is False
    assert config.require_human_approval is False  # default: no branch protection rules in autonomous setup
    assert config.max_improvements_per_day == 3
    assert config.max_changed_files_per_pr == 3
    assert config.max_lines_changed_per_pr == 200
    assert "config.py" in config.forbidden_modification_paths
    assert "improvement.py" in config.forbidden_modification_paths


# -- validate_import_policy --------------------------------------------------

def test_validate_import_policy_allows_clean_source():
    source = "import json\nimport os.path\nfrom pathlib import Path\n"
    assert validate_import_policy("src/ouroboros/x.py", source) == []


@pytest.mark.parametrize(
    "source",
    [
        "import ctypes\n",
        "import pickle\n",
        "import socket\n",
        "import marshal\n",
        "import shelve\n",
        "import pty\n",
    ],
)
def test_validate_import_policy_rejects_each_default_module(source):
    violations = validate_import_policy("src/ouroboros/x.py", source)
    assert len(violations) == 1
    assert "Forbidden import" in violations[0]


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("import pickle\n", "pickle"),
        ("import pickle as p\n", "pickle"),
        ("import socket.foo\n", "socket.foo"),          # submodule of a blocked root
        ("from pickle import loads\n", "pickle"),
        ("from socket import socket\n", "socket"),
        ("from ctypes.util import find_library\n", "ctypes.util"),
    ],
)
def test_validate_import_policy_catches_every_import_form(source, module):
    violations = validate_import_policy("src/ouroboros/x.py", source)
    assert len(violations) == 1
    assert module in violations[0]


@pytest.mark.parametrize(
    "source",
    [
        "import socketserver\n",       # not a submodule of "socket"
        "import pickletools\n",        # shares a prefix but is a distinct module
        "from socketserver import TCPServer\n",
    ],
)
def test_validate_import_policy_does_not_match_on_bare_prefix(source):
    assert validate_import_policy("src/ouroboros/x.py", source) == []


@pytest.mark.parametrize(
    "source",
    [
        "from . import pickle\n",
        "from .. import socket\n",
        "from .pickle import loads\n",
    ],
)
def test_validate_import_policy_ignores_relative_imports(source):
    """`from . import pickle` names a package-local module, not stdlib pickle."""
    assert validate_import_policy("src/ouroboros/x.py", source) == []


def test_validate_import_policy_reports_line_numbers():
    source = "import json\n\nimport pickle\n"
    violations = validate_import_policy("src/ouroboros/x.py", source)
    assert violations == [
        "Forbidden import: src/ouroboros/x.py:3 imports pickle "
        "(pickle is not permitted)"
    ]


def test_validate_import_policy_reports_every_violation():
    source = "import pickle\nimport socket\nimport json\n"
    violations = validate_import_policy("src/ouroboros/x.py", source)
    assert len(violations) == 2


def test_validate_import_policy_finds_nested_imports():
    """An import inside a function body is still an import."""
    source = "def f():\n    import pickle\n    return pickle\n"
    assert len(validate_import_policy("src/ouroboros/x.py", source)) == 1


def test_validate_import_policy_flags_unparseable_source():
    violations = validate_import_policy("src/ouroboros/x.py", "def broken(\n")
    assert len(violations) == 1
    assert "Unparseable" in violations[0]


def test_validate_import_policy_respects_custom_config():
    config = SafetyConfig(forbidden_import_modules=("json",))
    assert validate_import_policy("x.py", "import pickle\n", config) == []
    assert len(validate_import_policy("x.py", "import json\n", config)) == 1


def test_validate_import_policy_empty_blocklist_allows_everything():
    config = SafetyConfig(forbidden_import_modules=())
    assert validate_import_policy("x.py", "import pickle\nimport ctypes\n", config) == []


def test_validate_import_policy_empty_blocklist_skips_syntax_check():
    """With nothing to enforce there is no reason to parse at all."""
    config = SafetyConfig(forbidden_import_modules=())
    assert validate_import_policy("x.py", "def broken(\n", config) == []


def test_default_forbidden_imports_do_not_flag_the_existing_codebase():
    """The shipped blocklist must not reject code that is already in the repo."""
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted((repo_root / "src").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        offenders.extend(validate_import_policy(str(path), source))
    assert offenders == []


@pytest.mark.parametrize(
    ("source", "call"),
    [
        ('__import__("pickle")\n', "__import__"),
        ('import importlib\nimportlib.import_module("pickle")\n', "import_module"),
        ('exec("import pickle")\n', "exec"),
        ("eval(\"__import__('socket')\")\n", "eval"),
    ],
)
def test_validate_import_policy_flags_dynamic_import_primitives(source, call):
    """A blocklist is worthless if one line of indirection sidesteps it."""
    violations = validate_import_policy("src/ouroboros/x.py", source)
    assert any("Dynamic import" in v and call in v for v in violations)


def test_validate_import_policy_dotted_entry_catches_both_import_forms():
    """`from urllib import request` loads the same module as `import urllib.request`."""
    config = SafetyConfig(forbidden_import_modules=("urllib.request",))

    assert len(validate_import_policy("x.py", "import urllib.request\n", config)) == 1
    assert len(validate_import_policy("x.py", "from urllib import request\n", config)) == 1
    # The parent package on its own is still allowed.
    assert validate_import_policy("x.py", "import urllib\n", config) == []


def test_validate_import_policy_does_not_double_report_one_statement():
    """from-import expansion must not turn one bad line into two violations."""
    violations = validate_import_policy("x.py", "from pickle import loads, dumps\n")
    assert len(violations) == 1


def test_validate_import_policy_star_import():
    assert len(validate_import_policy("x.py", "from pickle import *\n")) == 1


def test_validate_import_policy_handles_null_bytes():
    violations = validate_import_policy("x.py", "import json\x00\n")
    assert len(violations) == 1
    assert "Unparseable" in violations[0]


def test_default_forbidden_imports_do_not_flag_the_test_suite():
    """The blocklist must not reject the repo's own tests either."""
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted((repo_root / "tests").rglob("*.py")):
        offenders.extend(
            validate_import_policy(str(path), path.read_text(encoding="utf-8"))
        )
    assert offenders == []


@pytest.mark.parametrize(
    "source",
    [
        "from importlib import import_module\nimport_module('pickle')\n",
        "from importlib import import_module as load\nload('pickle')\n",
        "from importlib.util import module_from_spec\n",  # binding form, no call
    ],
)
def test_validate_import_policy_tracks_import_module_bindings(source):
    """`from importlib import import_module` is ordinary style, not a trick."""
    violations = validate_import_policy("src/ouroboros/x.py", source)
    if "(" in source.split("\n", 1)[1]:
        assert any("Dynamic import" in v for v in violations)


def test_validate_import_policy_does_not_flag_unrelated_same_named_call():
    """A local helper that merely shares a builtin's name is not a call to it."""
    source = "def render(template):\n    return template\n\nrender('x')\n"
    assert validate_import_policy("src/ouroboros/x.py", source) == []


@pytest.mark.parametrize(
    "source",
    [
        "import builtins\nbuiltins.__import__('pickle')\n",
        "import builtins\nbuiltins.eval('1')\n",
        "import builtins\nbuiltins.exec('import pickle')\n",
        "from builtins import __import__ as load\nload('pickle')\n",
        "from builtins import __import__\n__import__('pickle')\n",
    ],
)
def test_validate_import_policy_flags_builtins_qualified_loaders(source):
    violations = validate_import_policy("src/ouroboros/x.py", source)
    assert any("Dynamic import" in v for v in violations), source
