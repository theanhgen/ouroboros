"""Self-benchmarking -- track improvement metrics over time."""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

METRICS_FILE = "config/metrics.json"
MAX_SNAPSHOTS = 200


def _metrics_path(repo_root: Path) -> Path:
    return repo_root / METRICS_FILE


def _coerce_snapshots(data: Any) -> Optional[List[Dict[str, Any]]]:
    """Return the snapshot list in a loaded document, or None if it holds none.

    None and [] are different answers and the difference matters on the write
    path: [] means "a readable file with no snapshots yet", None means "this
    document is not a metrics file", and only the first is safe to append to
    and write back.
    """
    if isinstance(data, dict):
        snapshots = data.get("snapshots")
    elif isinstance(data, list):
        # The original on-disk shape was a bare list. Still readable.
        snapshots = data
    else:
        snapshots = None
    return snapshots if isinstance(snapshots, list) else None


def _quarantine_corrupt(path: Path) -> Path:
    """Move an unreadable metrics file aside instead of letting it be rewritten.

    A history that cannot be parsed is still worth more sitting on disk than
    replaced by a single fresh row: the file is the only copy, config/ is
    auto-committed every cycle, and a truncated series is indistinguishable
    from an agent that has just run for the first time.

    Failing to move it aside raises rather than falling through, because the
    caller's next step is the write that would destroy it.
    """
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    dest = path.with_name(f"{path.name}.corrupt-{stamp}")
    suffix = 1
    while dest.exists():
        dest = path.with_name(f"{path.name}.corrupt-{stamp}-{suffix}")
        suffix += 1
    os.replace(path, dest)
    log.error(
        "%s could not be read and was moved to %s; the history it held is not "
        "in the new file", path, dest.name,
    )
    return dest


def load_metrics(repo_root: Path) -> List[Dict[str, Any]]:
    path = _metrics_path(repo_root)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, KeyError):
        return []
    snapshots = _coerce_snapshots(data)
    return snapshots if snapshots is not None else []


def save_metrics(repo_root: Path, snapshots: List[Dict[str, Any]]) -> None:
    path = _metrics_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep bounded
    if len(snapshots) > MAX_SNAPSHOTS:
        snapshots = snapshots[-MAX_SNAPSHOTS:]
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"snapshots": snapshots}, f, indent=2)
    os.replace(tmp, str(path))


def _first_attr(obj: Any, names: List[str]) -> Any:
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def _serialize_policy_result(result: Any) -> Optional[Dict[str, Any]]:
    if result is None:
        return None

    data = {}
    for attr in (
        "file_paths",
        "allowed_prefixes",
        "forbidden_paths",
        "num_files",
        "max_files",
        "num_lines",
        "max_lines",
    ):
        if hasattr(result, attr):
            value = getattr(result, attr)
            data[attr] = list(value) if isinstance(value, tuple) else value

    violations = getattr(result, "violations", None)
    if violations is None and isinstance(result, list):
        violations = result
    if violations is not None:
        data["violations"] = list(violations)

    if hasattr(result, "is_valid"):
        data["is_valid"] = bool(getattr(result, "is_valid"))
    elif "violations" in data:
        data["is_valid"] = len(data["violations"]) == 0

    return data or None


def _derive_policy_results(improvement_result: Any) -> Dict[str, Any]:
    changes = getattr(improvement_result, "changes", None)
    if changes is None:
        return {}

    from .improvement import _count_changed_lines
    from .policies import validate_change_size, validate_modification_scope

    file_paths = [getattr(change, "file_path", "") for change in changes]
    num_lines = sum(
        _count_changed_lines(
            getattr(change, "original_content", ""),
            getattr(change, "new_content", ""),
        )
        for change in changes
    )

    return {
        "policy_scope": validate_modification_scope(file_paths),
        "policy_size": validate_change_size(len(file_paths), num_lines),
    }


def _policy_metrics(improvement_result: Any) -> Dict[str, Dict[str, Any]]:
    scope = _first_attr(
        improvement_result,
        ["policy_scope_result", "modification_scope_result"],
    )
    size = _first_attr(
        improvement_result,
        ["policy_size_result", "change_size_result"],
    )

    if scope is None or size is None:
        derived = _derive_policy_results(improvement_result)
        scope = scope if scope is not None else derived.get("policy_scope")
        size = size if size is not None else derived.get("policy_size")

    metrics = {}
    serialized_scope = _serialize_policy_result(scope)
    serialized_size = _serialize_policy_result(size)
    if serialized_scope is not None:
        metrics["policy_scope"] = serialized_scope
    if serialized_size is not None:
        metrics["policy_size"] = serialized_size
    return metrics


def _append_snapshot(repo_root: Path, snapshot: Dict[str, Any]) -> None:
    """Add one snapshot to the history, under the storage lock.

    load-append-save as three statements lets a second process write between
    the load and the save, dropping whichever record was appended first, and --
    worse -- makes load_metrics' "return [] for a file I cannot read" into a
    wholesale truncation, because the empty list is written straight back over
    the file it came from. update_json_file closes the race; the quarantine
    hook and the None check below keep an unreadable history off the write
    path entirely.
    """
    from .storage import update_json_file

    path = _metrics_path(repo_root)

    def append(data: Any) -> Dict[str, Any]:
        snapshots = _coerce_snapshots(data)
        if snapshots is None:
            # Parsed, but not a metrics document. Same treatment as a parse
            # failure: whatever it is, it is not ours to overwrite.
            _quarantine_corrupt(path)
            snapshots = []
        snapshots.append(snapshot)
        return {"snapshots": snapshots[-MAX_SNAPSHOTS:]}

    update_json_file(
        path,
        append,
        default={"snapshots": []},
        replace=True,
        on_corrupt=_quarantine_corrupt,
    )


def record_snapshot(
    repo_root: Path,
    improvement_result: Any = None,
) -> Dict[str, Any]:
    """Record a metrics snapshot after an improvement cycle."""
    from .evaluation import load_history
    from .test_runner import run_tests

    history = load_history(repo_root)

    # Count source lines
    src_lines = 0
    test_lines = 0
    src_dir = repo_root / "src"
    test_dir = repo_root / "tests"
    for d, counter_name in [(src_dir, "src"), (test_dir, "test")]:
        if d.exists():
            for py in d.rglob("*.py"):
                try:
                    count = len(py.read_text(encoding="utf-8").splitlines())
                    if counter_name == "src":
                        src_lines += count
                    else:
                        test_lines += count
                except Exception:
                    pass

    # Calculate success rate over last 30 days
    cutoff_30d = time.time() - 30 * 86400
    recent = [r for r in history if r.timestamp > cutoff_30d]
    total_attempts = len(recent)
    successes = sum(1 for r in recent if r.outcome in ("merged", "success"))
    success_rate = (successes / total_attempts * 100) if total_attempts else 0.0

    snapshot = {
        "timestamp": time.time(),
        "src_lines": src_lines,
        "test_lines": test_lines,
        "total_improvements": len(history),
        "recent_attempts_30d": total_attempts,
        "recent_successes_30d": successes,
        "success_rate_30d": round(success_rate, 1),
    }

    if improvement_result is not None:
        snapshot["last_task_type"] = getattr(
            getattr(improvement_result, "task", None), "task_type", "unknown"
        )
        snapshot["last_status"] = getattr(improvement_result, "status", "unknown")
        test_after = getattr(improvement_result, "test_after", None)
        if test_after:
            snapshot["tests_passed"] = test_after.passed
            snapshot["tests_failed"] = test_after.failed
        snapshot.update(_policy_metrics(improvement_result))

    _append_snapshot(repo_root, snapshot)

    log.info(
        "[metrics] Snapshot: %d src LOC, %d test LOC, %.1f%% success rate (30d)",
        src_lines, test_lines, success_rate,
    )
    return snapshot


def get_summary(repo_root: Path) -> str:
    """Generate a human-readable metrics summary."""
    snapshots = load_metrics(repo_root)
    if not snapshots:
        return "No metrics recorded yet."

    latest = snapshots[-1]
    lines = [
        f"Source: {latest.get('src_lines', 0)} LOC",
        f"Tests: {latest.get('test_lines', 0)} LOC",
        f"Total improvements: {latest.get('total_improvements', 0)}",
        f"Success rate (30d): {latest.get('success_rate_30d', 0)}%",
        f"  ({latest.get('recent_successes_30d', 0)}/{latest.get('recent_attempts_30d', 0)})",
    ]

    # Trend: compare with snapshot from ~7 days ago
    week_ago = time.time() - 7 * 86400
    older = [s for s in snapshots if s.get("timestamp", 0) < week_ago]
    if older:
        prev = older[-1]
        src_delta = latest.get("src_lines", 0) - prev.get("src_lines", 0)
        test_delta = latest.get("test_lines", 0) - prev.get("test_lines", 0)
        rate_delta = latest.get("success_rate_30d", 0) - prev.get("success_rate_30d", 0)
        lines.append(f"7d trend: src {src_delta:+d} LOC, tests {test_delta:+d} LOC, rate {rate_delta:+.1f}%")

    return "\n".join(lines)
