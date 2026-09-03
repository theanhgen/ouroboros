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


# -- the source describes the behaviour (#51) --------------------------------

def test_the_guarded_modules_own_their_own_functions():
    """These were rebound from __init__.py at package-import time, so reading
    policies.py did not tell you what validate_modification_scope did."""
    import inspect

    from ouroboros import improvement, policies

    for fn, expected in (
        (policies.validate_modification_scope, "policies.py"),
        (policies.validate_change_size, "policies.py"),
        (improvement._is_path_allowed, "improvement.py"),
    ):
        assert inspect.getsourcefile(fn).endswith(expected), (
            f"{fn.__name__} is defined in "
            f"{inspect.getsourcefile(fn)}, not {expected}"
        )


def test_importing_the_package_mutates_nothing():
    """Import-time rebinding made import order semantically significant."""
    import ast
    from pathlib import Path

    import ouroboros

    source = Path(ouroboros.__file__).read_text()
    for node in ast.parse(source).body:
        assert isinstance(node, (ast.Assign, ast.Expr, ast.Import, ast.ImportFrom)), (
            f"__init__.py runs {type(node).__name__} at import time"
        )
        if isinstance(node, ast.Expr):
            assert isinstance(node.value, ast.Constant), "no calls at import time"


# -- one definition of what is immutable -------------------------------------

def test_both_gates_agree_on_a_forbidden_path():
    """They were kept aligned by patching both from outside. Sharing the helper
    is what makes them unable to disagree."""
    from dataclasses import replace

    from ouroboros import improvement
    from ouroboros.config import SafetyConfig
    from ouroboros.policies import validate_modification_scope

    config = replace(
        SafetyConfig(),
        forbidden_modification_paths=("src/ouroboros/policies.py",),
        allowed_modification_paths=("src/",),
    )
    target = "src/ouroboros/policies.py"

    assert improvement._is_path_allowed(target, config) is False
    assert list(validate_modification_scope([target], config)), (
        "the scope check let through what the improvement gate refused"
    )


def test_a_forbidden_prefix_covers_everything_under_it():
    """The shipped config lists filenames; the prefix arm is what lets an
    operator forbid a directory without naming every file in it."""
    from ouroboros.policies import is_forbidden_modification_path

    forbidden = ("src/ouroboros/internal/",)
    assert is_forbidden_modification_path(
        "src/ouroboros/internal/deep/thing.py", forbidden
    )
    assert not is_forbidden_modification_path("src/ouroboros/other.py", forbidden)


def test_a_bare_filename_is_matched_anywhere_in_the_tree():
    from ouroboros.policies import is_forbidden_modification_path

    assert is_forbidden_modification_path("src/ouroboros/config.py", ("config.py",))
    assert is_forbidden_modification_path("config.py", ("config.py",))
    assert not is_forbidden_modification_path("src/settings.py", ("config.py",))


# -- the decision carries what it was made against ---------------------------

def test_policy_results_are_lists_of_violations_first():
    """Every existing caller treats the return value as the violations."""
    from ouroboros.policies import validate_change_size, validate_modification_scope

    clean = validate_modification_scope(["src/ouroboros/x.py"])
    assert clean == [] and not clean and clean.is_valid

    over = validate_change_size(99, 9999)
    assert len(over) == 2 and not over.is_valid
    assert over.violations == list(over)


def test_metrics_still_records_the_inputs_to_the_decision():
    """The structured fields exist for this; a plain list would thin it."""
    from ouroboros.metrics import _serialize_policy_result
    from ouroboros.policies import validate_change_size, validate_modification_scope

    size = _serialize_policy_result(validate_change_size(99, 9999))
    assert {"num_files", "max_files", "num_lines", "max_lines"} <= set(size)

    scope = _serialize_policy_result(
        validate_modification_scope(["policies.py"])
    )
    assert {"file_paths", "allowed_prefixes", "forbidden_paths"} <= set(scope)
    assert scope["is_valid"] is False


# -- path matching is on components, after normalisation ---------------------

@pytest.mark.parametrize("path", [
    "src/ouroboros/internal/x.py",
    "./src/ouroboros/internal/x.py",
    "src/ouroboros/./internal/x.py",
    "src/ouroboros/other/../internal/x.py",
])
def test_a_forbidden_prefix_is_not_dodged_by_an_unnormalised_path(path):
    from ouroboros.policies import is_forbidden_modification_path

    assert is_forbidden_modification_path(path, ("src/ouroboros/internal/",))


def test_a_prefix_matches_whole_components_only():
    """"src/a" must not claim "src/ab", and "policies.py" must not claim
    "policies.pyx"."""
    from ouroboros.policies import is_forbidden_modification_path

    assert not is_forbidden_modification_path("src/ab/x.py", ("src/a/",))
    assert not is_forbidden_modification_path("src/policies.pyx", ("policies.py",))
    assert is_forbidden_modification_path("src/a/x.py", ("src/a/",))


def test_scope_judges_the_same_normalised_path_as_the_forbidden_check():
    """Otherwise one path is measured by two different standards."""
    from ouroboros.policies import validate_modification_scope

    assert list(validate_modification_scope(["./src/ouroboros/x.py"])) == []
    assert list(validate_modification_scope(["src/ouroboros/../x.py"])), (
        "escaping the allowed prefix was accepted"
    )


# -- the two gates are one implementation ------------------------------------

ADVERSARIAL_PATHS = [
    ("src/ouroboros/x.py", True),
    ("./src/ouroboros/x.py", True),
    ("src/ouroboros/./x.py", True),
    ("tests/test_x.py", True),
    ("docs/wiki/x.md", True),
    # Traversal that satisfies a prefix by string comparison while naming a
    # file outside the repository.
    ("src/../../etc/passwd", False),
    ("src/ouroboros/../../../etc/passwd", False),
    # `repo_root / "/etc/passwd"` is "/etc/passwd".
    ("/etc/passwd", False),
    ("//etc/passwd", False),
    ("C:\\Windows\\x.py", False),
    ("src\\..\\..\\etc\\passwd", False),
    ("..", False),
    (".", False),
    ("", False),
    # Immutable.
    ("src/ouroboros/config.py", False),
    ("config.py", False),
    # A prefix must not claim a sibling that merely starts with the same text.
    ("src/ouroborosevil/x.py", False),
]


@pytest.mark.parametrize(("path", "expected"), ADVERSARIAL_PATHS)
def test_both_gates_reach_the_same_verdict(path, expected):
    """They were separate implementations kept in step by hand, and drifted:
    validate_modification_scope normalised the path and _is_path_allowed
    compared raw strings, so "src/../../etc/passwd" was refused by one and
    accepted by the other."""
    from ouroboros.config import SafetyConfig
    from ouroboros.improvement import _is_path_allowed
    from ouroboros.policies import validate_modification_scope

    config = SafetyConfig()
    allowed = _is_path_allowed(path, config)
    in_scope = not list(validate_modification_scope([path], config))

    assert allowed == in_scope, (
        f"_is_path_allowed={allowed} but scope-clean={in_scope} for {path!r}"
    )
    assert allowed is expected


def test_a_traversal_change_never_reaches_apply(monkeypatch, tmp_path):
    from ouroboros import improvement
    from ouroboros.config import SafetyConfig
    from ouroboros.improvement import CodeChange, ImprovementTask
    from ouroboros.test_runner import RunnerOutcome

    applied = []
    monkeypatch.setattr(
        improvement, "apply_changes", lambda *a, **kw: applied.append(a) or True
    )
    monkeypatch.setattr(
        improvement, "run_tests",
        lambda *a, **kw: RunnerOutcome(passed=1, failed=0, errors=0, returncode=0),
    )

    result = improvement.validate_improvement(
        ImprovementTask("t1", "fix_bug", "d", [], "e"),
        [CodeChange(
            file_path="src/../../etc/cron.d/x",
            original_content="",
            new_content="* * * * * root sh\n",
            description="d",
        )],
        tmp_path,
        config=SafetyConfig(),
    )

    assert applied == []
    assert result.status == "failed"


# -- the gate answers "which file", not "are these strings equal" ------------

@pytest.mark.parametrize("path", [
    "src/ouroboros/CONFIG.PY",
    "src/ouroboros/Config.Py",
    "src/ouroboros/config.PY",
])
def test_a_forbidden_file_cannot_be_reached_by_changing_its_case(path):
    """APFS and NTFS are case-insensitive, so this opens the real config.py
    while comparing unequal to it."""
    from ouroboros.config import SafetyConfig
    from ouroboros.improvement import _is_path_allowed
    from ouroboros.policies import is_forbidden_modification_path

    config = SafetyConfig()
    assert is_forbidden_modification_path(
        path, config.forbidden_modification_paths
    )
    assert _is_path_allowed(path, config) is False


def test_a_forbidden_directory_cannot_be_reached_by_changing_its_case():
    from ouroboros.policies import is_forbidden_modification_path

    assert is_forbidden_modification_path(
        "SRC/Ouroboros/Internal/x.py", ("src/ouroboros/internal/",)
    )


def test_decomposed_unicode_matches_its_composed_twin():
    """macOS hands back NFD; a forbidden entry written NFC would miss it."""
    import unicodedata

    from ouroboros.policies import is_forbidden_modification_path

    composed = unicodedata.normalize("NFC", "café.py")
    decomposed = unicodedata.normalize("NFD", "café.py")
    assert composed != decomposed
    assert is_forbidden_modification_path(f"src/{decomposed}", (composed,))


def test_a_null_byte_is_not_a_path():
    """pathlib accepts it and the OS call then raises ValueError, which a
    caller does not expect from a path check."""
    from ouroboros.policies import is_safe_relative_path

    assert is_safe_relative_path("src/ouroboros/x.py\x00.md") is False


def _rejected_by_enforcement(paths, config):
    from ouroboros.improvement import _is_path_allowed

    return {p for p in paths if not _is_path_allowed(p, config)}


def _rejected_by_metrics_path(paths, config):
    return {p for p in paths if validate_modification_scope([p], config)}


# Every forbidden entry, plus a near-miss for each, plus an allowed file.
_SCOPE_FIXTURE = (
    "src/ouroboros/config.py",
    "src/ouroboros/improvement.py",
    "src/ouroboros/git_ops.py",
    "src/ouroboros/evaluation.py",
    "src/ouroboros/policies.py",
    "src/ouroboros/policies.pyx",
    "tests/config.py",
    "src/ab/thing.py",
    "src/ouroboros/llm.py",
    "README.md",
    "src/../../etc/passwd",
)


def test_enforcement_and_the_metrics_path_reject_the_same_files():
    """_is_path_allowed is the gate; validate_modification_scope is what
    metrics reports from. A file one rejects and the other accepts is a
    metric that contradicts what the agent was actually allowed to do."""
    config = SafetyConfig()
    assert (
        _rejected_by_enforcement(_SCOPE_FIXTURE, config)
        == _rejected_by_metrics_path(_SCOPE_FIXTURE, config)
    )
    # tests/config.py is forbidden by basename, so both must reject it; the
    # near-misses next to it must be accepted by both.
    assert "tests/config.py" in _rejected_by_enforcement(_SCOPE_FIXTURE, config)
    assert "src/ouroboros/llm.py" not in _rejected_by_enforcement(
        _SCOPE_FIXTURE, config
    )


def test_the_immutable_list_has_a_single_source():
    """The two gates read one list, so changing it moves both. With a second
    hardcoded copy in improvement.py, a config that no longer forbids
    improvement.py would still be enforced against while metrics reported the
    change as in scope (#112)."""
    config = SafetyConfig(forbidden_modification_paths=("secret.py",))
    paths = (
        "src/ouroboros/improvement.py",
        "src/ouroboros/policies.py",
        "src/ouroboros/secret.py",
        "src/ouroboros/llm.py",
    )
    assert (
        _rejected_by_enforcement(paths, config)
        == _rejected_by_metrics_path(paths, config)
        == {"src/ouroboros/secret.py"}
    )


# -- the filesystem answers what a lexical check cannot ----------------------

def test_a_symlinked_component_cannot_be_written_through(tmp_path):
    """Every prefix rule is satisfied lexically; only resolving the path shows
    it lands outside the repository."""
    import os

    from ouroboros.improvement import CodeChange, apply_changes

    repo = tmp_path / "repo"
    (repo / "src" / "ouroboros").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "secret.py"
    victim.write_text("original\n")

    os.symlink(outside, repo / "src" / "ouroboros" / "link")

    change = CodeChange(
        file_path="src/ouroboros/link/secret.py",
        original_content="",
        new_content="OWNED = 1\n",
        description="d",
    )
    with pytest.raises(PermissionError, match="escapes the repository"):
        apply_changes([change], repo)

    assert victim.read_text() == "original\n"


def test_an_ordinary_change_still_writes(tmp_path):
    from ouroboros.improvement import CodeChange, apply_changes

    repo = tmp_path / "repo"
    (repo / "src" / "ouroboros").mkdir(parents=True)
    apply_changes([CodeChange(
        file_path="src/ouroboros/new_thing.py",
        original_content="",
        new_content="VALUE = 1\n",
        description="d",
    )], repo)

    assert (repo / "src" / "ouroboros" / "new_thing.py").read_text() == "VALUE = 1\n"


def test_a_path_shaped_entry_does_not_claim_every_file_of_that_name():
    """A bare "config.py" means "anywhere in the tree"; a path means that one
    file, and must not blanket-block a same-named file elsewhere."""
    from ouroboros.policies import is_forbidden_modification_path

    entry = ("src/ouroboros/config.py",)
    assert is_forbidden_modification_path("src/ouroboros/config.py", entry)
    assert not is_forbidden_modification_path("tests/config.py", entry)

    # The shipped config uses bare names, which still match anywhere.
    assert is_forbidden_modification_path("tests/config.py", ("config.py",))


def test_a_symlink_inside_the_repo_cannot_reach_an_immutable_file(tmp_path):
    """Landing inside the repository is not enough. link.py resolves to
    config.py, which is inside and passes every name-based check, while the
    write goes to the agent's own SafetyConfig."""
    import os

    from ouroboros.improvement import CodeChange, apply_changes

    repo = tmp_path / "repo"
    pkg = repo / "src" / "ouroboros"
    pkg.mkdir(parents=True)
    (pkg / "config.py").write_text("ALLOW_SELF_MODIFICATION = False\n")
    os.symlink(pkg / "config.py", pkg / "link.py")

    with pytest.raises(PermissionError, match="forbidden"):
        apply_changes([CodeChange(
            file_path="src/ouroboros/link.py",
            original_content="",
            new_content="ALLOW_SELF_MODIFICATION = True\n",
            description="d",
        )], repo)

    assert (pkg / "config.py").read_text() == "ALLOW_SELF_MODIFICATION = False\n"


def test_apply_changes_uses_the_config_it_was_validated_against(tmp_path):
    """It built its own SafetyConfig, so a caller with custom allowed paths
    passed validation and was then refused at the write."""
    from dataclasses import replace

    from ouroboros.config import SafetyConfig
    from ouroboros.improvement import CodeChange, apply_changes

    repo = tmp_path / "repo"
    (repo / "custom").mkdir(parents=True)
    config = replace(SafetyConfig(), allowed_modification_paths=("custom/",))

    apply_changes([CodeChange(
        file_path="custom/x.py",
        original_content="",
        new_content="VALUE = 1\n",
        description="d",
    )], repo, config)

    assert (repo / "custom" / "x.py").read_text() == "VALUE = 1\n"


def test_a_trailing_slash_entry_is_a_directory_not_a_filename():
    """normpath strips it, after which "some_dir/" looks like a bare filename
    and would block every file named some_dir in the tree."""
    from ouroboros.policies import is_forbidden_modification_path

    assert is_forbidden_modification_path("some_dir/x.py", ("some_dir/",))
    assert not is_forbidden_modification_path("tests/some_dir", ("some_dir/",))
