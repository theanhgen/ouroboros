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


def load_metrics(repo_root: Path) -> List[Dict[str, Any]]:
    path = _metrics_path(repo_root)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("snapshots", []) if isinstance(data, dict) else data
    except (json.JSONDecodeError, KeyError):
        return []


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

    if improvement_result:
        snapshot["last_task_type"] = getattr(
            getattr(improvement_result, "task", None), "task_type", "unknown"
        )
        snapshot["last_status"] = getattr(improvement_result, "status", "unknown")
        if improvement_result.test_after:
            snapshot["tests_passed"] = improvement_result.test_after.passed
            snapshot["tests_failed"] = improvement_result.test_after.failed

    snapshots = load_metrics(repo_root)
    snapshots.append(snapshot)
    save_metrics(repo_root, snapshots)

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
