import ast
from dataclasses import dataclass
import ntpath
import posixpath
import unicodedata
from pathlib import Path, PurePosixPath
from typing import List, Optional, Tuple

from .config import SafetyConfig


@dataclass(frozen=True)
class Evidence:
    source: str
    location: str
    note: str


class PolicyError(RuntimeError):
    pass


class _PolicyDecision(list):
    """A list of violations that also carries what was decided and against what.

    A list first, so every caller that treats the result as "the violations"
    keeps working; the attributes exist because metrics records the inputs to
    the decision, not only its outcome.
    """

    @property
    def violations(self) -> List[str]:
        return list(self)

    @property
    def is_valid(self) -> bool:
        return len(self) == 0


class ModificationScopeResult(_PolicyDecision):
    def __init__(self, file_paths, allowed_prefixes, forbidden_paths, violations):
        super().__init__(violations)
        self.file_paths = list(file_paths)
        self.allowed_prefixes = list(allowed_prefixes)
        self.forbidden_paths = list(forbidden_paths)


class ChangeSizeResult(_PolicyDecision):
    def __init__(self, num_files, max_files, num_lines, max_lines, violations):
        super().__init__(violations)
        self.num_files = num_files
        self.max_files = max_files
        self.num_lines = num_lines
        self.max_lines = max_lines


def _normalised(file_path: str) -> PurePosixPath:
    """Collapse `.`, `..` and duplicate separators without touching the disk.

    posixpath rather than os.path so the result does not depend on the host
    separator, and normpath rather than resolve() so no filesystem lookup
    happens and a path that does not exist yet still normalises.
    """
    return PurePosixPath(posixpath.normpath(file_path.replace("\\", "/")))


def _comparison_key(file_path: str) -> PurePosixPath:
    """The form two paths are compared in.

    Case-folded and NFC-normalised, because the question these gates answer is
    "which file on disk does this name", not "are these strings equal". APFS
    and NTFS are case-insensitive, so "src/ouroboros/CONFIG.PY" opens the
    forbidden config.py while comparing unequal to it; macOS also hands back
    decomposed unicode, so an NFD name misses its NFC twin.

    On a case-sensitive filesystem this over-matches -- Config.py is genuinely
    a different file there -- which for a deny list is the safe direction.
    """
    text = unicodedata.normalize("NFC", file_path)
    return PurePosixPath(posixpath.normpath(text.replace("\\", "/")).casefold())


def is_safe_relative_path(file_path: str) -> bool:
    """Whether a path stays inside the tree it is resolved against.

    An absolute path is not "under" anything: `repo_root / "/etc/passwd"`
    discards repo_root entirely. A path that normalises to a leading ".."
    climbs out. Both must be refused outright rather than pattern-matched,
    because "src/../../etc/passwd" satisfies a "src/" prefix by string
    comparison while naming a file two levels above the repository.
    """
    if not file_path or "\x00" in file_path:
        # A null byte is not a path. pathlib accepts it and the OS call then
        # raises ValueError, which callers do not expect from a path check.
        return False
    cleaned = file_path.replace("\\", "/")
    if posixpath.isabs(cleaned) or ntpath.isabs(cleaned):
        return False
    normalised = _normalised(cleaned)
    return not (normalised.parts and normalised.parts[0] == "..")


def is_within_allowed_paths(
    file_path: str,
    allowed_paths: Tuple[str, ...],
) -> bool:
    """Whether a path is under one of the allowed prefixes.

    The single implementation behind both gates. They used to check this
    separately -- one normalised and one did not -- which is how they came to
    disagree about "src/../../etc/passwd".
    """
    if not is_safe_relative_path(file_path):
        return False

    candidate = _comparison_key(file_path)
    for prefix in allowed_paths:
        allowed = _comparison_key(prefix)
        if candidate == allowed or allowed in candidate.parents:
            return True
    return False


def is_forbidden_modification_path(
    file_path: str,
    forbidden_paths: Tuple[str, ...],
) -> bool:
    """Whether a path is off limits for modification.

    Entries may be a bare filename or a path prefix. The shipped config uses
    filenames, so the prefix arm is what lets an operator forbid a directory
    without listing every file in it.

    Both sides are normalised first: "./src/x.py" and "src/./x.py" name the
    same file as "src/x.py", and a raw string compare would let either past a
    prefix entry. Matching is on whole path components, so "policies.py" does
    not also claim "policies.pyx" and "src/a" does not claim "src/ab".
    """
    candidate = _comparison_key(file_path)
    folded_names = {_comparison_key(entry).name for entry in forbidden_paths}
    if candidate.name in folded_names:
        return True

    for forbidden_path in forbidden_paths:
        target = _comparison_key(forbidden_path)
        if candidate == target or target in candidate.parents:
            return True
    return False


def require_pr_only(is_pr_only: bool) -> None:
    if not is_pr_only:
        raise PolicyError("PR-only policy violated")


def validate_modification_scope(
    file_paths: List[str],
    config: SafetyConfig | None = None,
) -> ModificationScopeResult:
    """Validate that all file paths are within allowed modification scope.

    The result is a list of violation messages (empty = all paths valid) that
    also carries what was checked, for metrics.
    """
    config = config or SafetyConfig()
    violations = []

    for file_path in file_paths:
        basename = Path(file_path).name

        if not is_safe_relative_path(file_path):
            violations.append(
                f"Unsafe path: {file_path} (must be relative and inside the tree)"
            )
            continue

        # Check forbidden files
        if is_forbidden_modification_path(
            file_path, config.forbidden_modification_paths
        ):
            violations.append(f"Forbidden file: {file_path} ({basename} is immutable)")
            continue

        if not is_within_allowed_paths(
            file_path, config.allowed_modification_paths
        ):
            violations.append(
                f"Out of scope: {file_path} (must be under {config.allowed_modification_paths})"
            )

    return ModificationScopeResult(
        file_paths=file_paths,
        allowed_prefixes=config.allowed_modification_paths,
        forbidden_paths=config.forbidden_modification_paths,
        violations=violations,
    )


def _imported_module_names(tree: ast.AST) -> List[Tuple[str, int]]:
    """Return (dotted module name, line number) for every import in the tree.

    A from-import contributes both the module it reads from and each candidate
    submodule, because `from urllib import request` and `import urllib.request`
    load the same thing and a dotted blocklist entry must catch both. The
    submodule form over-reports when the name is an attribute rather than a
    module, which only matters for dotted blocklist entries.

    Relative imports are skipped: `from . import x` names a module inside this
    package, not the top-level module `x`, so it cannot match the blocklist.
    """
    names: List[Tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.append((node.module, node.lineno))
                names.extend(
                    (f"{node.module}.{alias.name}", node.lineno)
                    for alias in node.names
                    if alias.name != "*"
                )
    return names


# Primitives that load a module named at runtime, which no static check can
# resolve. Flagged so the blocklist cannot be sidestepped by a one-line
# rewrite; none of them is used anywhere in src/ today.
_DYNAMIC_IMPORT_CALLS = ("__import__", "eval", "exec")
_DYNAMIC_IMPORT_ATTRS = ("import_module",)


_DYNAMIC_PRIMITIVES = _DYNAMIC_IMPORT_CALLS + _DYNAMIC_IMPORT_ATTRS

# Modules that re-export a dynamic loader under a bindable name.
_DYNAMIC_LOADER_SOURCES = ("importlib", "builtins")


def _dynamic_loader_bindings(tree: ast.AST) -> set:
    """Return local names bound to a dynamic loader by a from-import.

    `from importlib import import_module` and `from builtins import
    __import__ as load` both produce a plain call that no attribute check
    would see.
    """
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module and node.module.split(".")[0] in _DYNAMIC_LOADER_SOURCES:
                bound.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name in _DYNAMIC_PRIMITIVES
                )
    return bound


def _dynamic_import_calls(tree: ast.AST) -> List[Tuple[str, int]]:
    """Return (call name, line number) for runtime-import / eval primitives.

    Covers the bare name, an attribute access such as
    ``importlib.import_module`` or ``builtins.__import__``, and any local name
    a from-import bound to one of them. It does not chase a loader through an
    arbitrary expression -- see validate_import_policy's docstring on why this
    is a lint rather than a boundary.
    """
    bound = _dynamic_loader_bindings(tree)
    found: List[Tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and (
            func.id in _DYNAMIC_IMPORT_CALLS or func.id in bound
        ):
            found.append((func.id, node.lineno))
        elif isinstance(func, ast.Attribute) and func.attr in _DYNAMIC_PRIMITIVES:
            found.append((func.attr, node.lineno))
    return found


def _is_forbidden_import(module: str, forbidden: Tuple[str, ...]) -> Optional[str]:
    """Return the blocklist entry that module matches, or None.

    Matching is on dotted-path segments so "os" rejects "os.path" but "socket"
    does not reject an unrelated "socketserver".
    """
    for entry in forbidden:
        if module == entry or module.startswith(entry + "."):
            return entry
    return None


def validate_import_policy(
    file_path: str,
    source: str,
    config: SafetyConfig | None = None,
) -> List[str]:
    """Report imports of modules on SafetyConfig.forbidden_import_modules.

    Returns a list of violation messages (empty = valid).

    This is a lint-level guard, not a security boundary. It reads the source
    statically, so it constrains what generated code is *written* to do, not
    what a determined program *can* do: subprocess, os and urllib all remain
    importable, and a module name assembled at runtime is unknowable here.
    Runtime-import and eval primitives are reported for that reason -- it
    raises the cost of an accidental or casual bypass -- but real capability
    limits belong in a sandbox, not in this function.

    Source that does not parse is itself a violation: unparseable code cannot
    be reviewed, and the caller is about to write it to disk.
    """
    config = config or SafetyConfig()
    if not config.forbidden_import_modules:
        return []

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as e:
        return [f"Unparseable: {file_path} (line {e.lineno}: {e.msg})"]
    except (ValueError, RecursionError) as e:
        # e.g. embedded NUL on some versions, or pathological nesting.
        return [f"Unparseable: {file_path} ({type(e).__name__}: {e})"]

    violations = []
    seen = set()
    for module, lineno in _imported_module_names(tree):
        entry = _is_forbidden_import(module, config.forbidden_import_modules)
        if entry is not None and (lineno, entry) not in seen:
            seen.add((lineno, entry))
            violations.append(
                f"Forbidden import: {file_path}:{lineno} imports {module} "
                f"({entry} is not permitted)"
            )

    for name, lineno in _dynamic_import_calls(tree):
        violations.append(
            f"Dynamic import: {file_path}:{lineno} calls {name} "
            f"(runtime module loading defeats import policy)"
        )

    return violations


def validate_change_size(
    num_files: int,
    num_lines: int,
    config: SafetyConfig | None = None,
) -> ChangeSizeResult:
    """Validate that a change doesn't exceed size limits.

    The result is a list of violation messages (empty = valid) that also
    carries the limits it was measured against, for metrics.
    """
    config = config or SafetyConfig()
    violations = []

    if num_files > config.max_changed_files_per_pr:
        violations.append(
            f"Too many files: {num_files} > {config.max_changed_files_per_pr}"
        )
    if num_lines > config.max_lines_changed_per_pr:
        violations.append(
            f"Too many lines: {num_lines} > {config.max_lines_changed_per_pr}"
        )

    return ChangeSizeResult(
        num_files=num_files,
        max_files=config.max_changed_files_per_pr,
        num_lines=num_lines,
        max_lines=config.max_lines_changed_per_pr,
        violations=violations,
    )
