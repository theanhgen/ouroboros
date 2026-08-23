"""Codebase self-reader -- lets the agent inspect its own source code."""

import ast
import logging
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class FunctionMetadata:
    name: str
    args: List[str]
    docstring: Optional[str] = None
    line_start: int = 0
    line_end: int = 0


@dataclass
class ClassMetadata:
    name: str
    docstring: Optional[str] = None
    methods: List[FunctionMetadata] = field(default_factory=list)
    line_start: int = 0
    line_end: int = 0


@dataclass
class FileMetadata:
    path: str
    classes: List[ClassMetadata] = field(default_factory=list)
    functions: List[FunctionMetadata] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)


def _extract_args(args_node: ast.arguments) -> List[str]:
    """Extract parameter names from an AST arguments node in Python order."""
    params: List[str] = []
    params.extend(arg.arg for arg in getattr(args_node, "posonlyargs", []))
    params.extend(arg.arg for arg in args_node.args)
    if args_node.vararg is not None:
        params.append(f"*{args_node.vararg.arg}")
    params.extend(arg.arg for arg in args_node.kwonlyargs)
    if args_node.kwarg is not None:
        params.append(f"**{args_node.kwarg.arg}")
    return params


def extract_code_metadata(content: str, path: str = "") -> FileMetadata:
    """Extract structural metadata from Python code using AST."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return FileMetadata(path=path)

    metadata = FileMetadata(path=path)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                for n in node.names:
                    metadata.imports.append(n.name)
            else:
                metadata.imports.append(node.module or "")

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            metadata.functions.append(FunctionMetadata(
                name=node.name,
                args=_extract_args(node.args),
                docstring=ast.get_docstring(node),
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", node.lineno)
            ))
        elif isinstance(node, ast.ClassDef):
            cls = ClassMetadata(
                name=node.name,
                docstring=ast.get_docstring(node),
                line_start=node.lineno,
                line_end=getattr(node, "end_lineno", node.lineno)
            )
            for subnode in node.body:
                if isinstance(subnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    cls.methods.append(FunctionMetadata(
                        name=subnode.name,
                        args=_extract_args(subnode.args),
                        docstring=ast.get_docstring(subnode),
                        line_start=subnode.lineno,
                        line_end=getattr(subnode, "end_lineno", subnode.lineno)
                    ))
            metadata.classes.append(cls)

    return metadata


def get_repo_root() -> Path:
    """Return the git repository root directory."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            return Path(out)
    except Exception:
        pass

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            return parent
    return here.parents[2]


def list_source_files(repo_root: Path | None = None) -> List[Path]:
    """Return all .py files under src/ouroboros/."""
    root = repo_root or get_repo_root()
    src_dir = root / "src" / "ouroboros"
    if not src_dir.exists():
        return []
    return sorted(src_dir.rglob("*.py"))


def get_test_files(repo_root: Path | None = None) -> List[Path]:
    """Return all test .py files under tests/."""
    root = repo_root or get_repo_root()
    test_dir = root / "tests"
    if not test_dir.exists():
        return []
    return sorted(test_dir.rglob("*.py"))


def read_file(path: Path) -> str:
    """Read file contents with line numbers prepended."""
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    numbered = [f"{i + 1:4d} | {line}" for i, line in enumerate(lines)]
    return "\n".join(numbered)


def read_file_raw(path: Path) -> str:
    """Read file contents without line numbers."""
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    return path.read_text(encoding="utf-8")


def get_function_signatures(path: Path) -> List[Dict[str, Any]]:
    """Extract function/method signatures using AST parsing.

    Returns list of dicts with keys: name, args, line, type ('function' or 'method'),
    and optionally 'class' for methods.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        log.warning("Failed to parse %s", path)
        return []

    signatures = []

    # Breadth-first, carrying the enclosing class down to each child. This
    # mirrors ast.walk's traversal order while tracking the parent, so a
    # function's owning class is known when it is reached instead of being
    # rediscovered by re-walking the whole tree per function.
    queue = deque([(tree, None)])
    while queue:
        node, owner = queue.popleft()

        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            sig: Dict[str, Any] = {
                "name": node.name,
                "args": _extract_args(node.args),
                "line": node.lineno,
                "type": "function",
            }
            # Only a direct child of a ClassDef body is a method: a function
            # nested inside another function, or under an `if` in a class
            # body, stays a plain function.
            if owner is not None:
                sig["type"] = "method"
                sig["class"] = owner
            signatures.append(sig)

        child_owner = node.name if isinstance(node, ast.ClassDef) else None
        queue.extend((child, child_owner) for child in ast.iter_child_nodes(node))

    return signatures


def get_codebase_summary(repo_root: Path | None = None) -> str:
    """Build an LLM-consumable summary of all source modules.

    Includes module names, classes, functions with signatures, and line counts.
    """
    root = repo_root or get_repo_root()
    src_files = list_source_files(root)
    test_files = get_test_files(root)

    parts = ["# Codebase Summary\n"]
    parts.append("## Source Files (src/ouroboros/)\n")

    for f in src_files:
        rel = f.relative_to(root)
        content = f.read_text(encoding="utf-8")
        line_count = len(content.splitlines())
        parts.append(f"### {rel} ({line_count} lines)")

        meta = extract_code_metadata(content, str(rel))
        
        if meta.classes:
            for cls in meta.classes:
                parts.append(f"  class {cls.name}")
                if cls.docstring:
                    parts.append(f"    \"\"\"{cls.docstring[:100]}...\"\"\"")
                for method in cls.methods:
                    args_str = ", ".join(method.args)
                    parts.append(f"    def {method.name}({args_str})")

        if meta.functions:
            for func in meta.functions:
                args_str = ", ".join(func.args)
                parts.append(f"  def {func.name}({args_str})")
        parts.append("")

    parts.append("## Test Files (tests/)\n")
    for f in test_files:
        rel = f.relative_to(root)
        line_count = len(f.read_text(encoding="utf-8").splitlines())
        parts.append(f"### {rel} ({line_count} lines)")

        sigs = get_function_signatures(f)
        if sigs:
            for sig in sigs:
                args_str = ", ".join(sig["args"])
                parts.append(f"  {sig['name']}({args_str}) [line {sig['line']}]")
        parts.append("")

    return "\n".join(parts)
