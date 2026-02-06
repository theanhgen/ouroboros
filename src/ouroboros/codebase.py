"""Codebase self-reader -- lets the agent inspect its own source code."""

import ast
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger(__name__)


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

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = []
            for arg in node.args.args:
                args.append(arg.arg)

            sig: Dict[str, Any] = {
                "name": node.name,
                "args": args,
                "line": node.lineno,
                "type": "function",
            }

            # Check if this is a method (parent is a ClassDef)
            for parent_node in ast.walk(tree):
                if isinstance(parent_node, ast.ClassDef):
                    for child in ast.iter_child_nodes(parent_node):
                        if child is node:
                            sig["type"] = "method"
                            sig["class"] = parent_node.name
                            break

            signatures.append(sig)

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
        line_count = len(f.read_text(encoding="utf-8").splitlines())
        parts.append(f"### {rel} ({line_count} lines)")

        sigs = get_function_signatures(f)
        if sigs:
            for sig in sigs:
                prefix = f"  {sig['class']}." if sig.get("class") else "  "
                args_str = ", ".join(sig["args"])
                parts.append(f"{prefix}{sig['name']}({args_str}) [line {sig['line']}]")
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
