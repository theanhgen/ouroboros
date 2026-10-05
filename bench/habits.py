"""Classify a bench run's misses into recurring habits. Deterministic, no LLM.

Each task in a cycle run gets exactly one signature: the first that matches,
in pipeline order. The table (counts, task ids, a line of evidence each) is
what the outer loop targets, the "habit with the rounds that show it".

  python bench/habits.py                 # newest cycle run
  python bench/habits.py <run-id> --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

# (signature, what it means) in pipeline order; the first match wins.
SIGNATURES = (
    ("invalid", "not scored: request budget, daily quota or auth ran out"),
    ("harness_error", "the harness or worker crashed"),
    ("resolved", "fixed: every F2P and P2P test passes"),
    ("out_of_scope", "identify picked a forbidden or out-of-scope file"),
    ("identify_error", "the identify call failed"),
    ("react_empty_final", "the ReAct loop's final answer was empty or not JSON"),
    ("named_no_task", "identify answered, but named no task"),
    ("no_task_other", "no task identified, for another or unlogged reason"),
    ("plan_failed", "no plan"),
    ("generate_truncated", "the edit reply hit max_tokens"),
    ("edit_mismatch", "SEARCH blocks did not match the file"),
    ("generate_empty", "no usable edit came back"),
    ("reviewer_rejected", "the peer reviewer rejected the change"),
    ("tests_regressed", "the cycle reverted its own change on a test regression"),
    ("false_merge", "the cycle would have merged, but the bug is not fixed"),
    ("partial_fix", "some F2P tests pass, not all"),
    ("wrong_fix", "changed code, fixed none of the F2P tests"),
    ("other", "none of the above"),
)
DESCRIPTIONS = dict(SIGNATURES)


def _f2p(row: dict) -> tuple[int, int]:
    try:
        done, total = str(row.get("f2p_passed", "0/0")).split("/")
        return int(done), int(total)
    except ValueError:
        return 0, 0


_REQUEST_RE = re.compile(r"bench: request \d+: .*tools=(True|False) .*-> finish=(\w+) "
                         r"content_chars=(\d+) tool_calls=(\d+)")


def request_note(log: str) -> str:
    """What the replies looked like, from run.py's per-request log lines.

    The 2026-10-05 diagnostic: offered no tools, north-mini-code still emits a
    tool call, which OpenRouter returns as finish=error with no content -- in
    every ReAct round and in the plan step.
    """
    shapes = _REQUEST_RE.findall(log)
    stray = sum(1 for tools, finish, _, calls in shapes if tools == "False" and int(calls))
    empty = sum(1 for _, finish, chars, calls in shapes if not int(chars) and not int(calls))
    parts = []
    if stray:
        parts.append(f"{stray} of {len(shapes)} requests: offered no tools, the model "
                     "replied with a tool call anyway (finish=error, no text)")
    if empty:
        parts.append(f"{empty} of {len(shapes)} requests: empty reply")
    return "; ".join(parts)


def _evidence(log: str, *needles: str) -> str:
    for line in log.splitlines():
        if any(n in line for n in needles):
            return line.strip()[:200]
    return ""


def classify(row: dict, log: str = "") -> tuple[str, str]:
    """(signature, one line of evidence) for one task's row and worker log."""
    details = str(row.get("details") or "")
    message = str(row.get("run_message") or "")
    status = row.get("cycle_status")
    text = f"{message}\n{details}\n{log}"
    if row.get("invalid"):
        return "invalid", (row.get("harness_error") or message)[:200]
    if row.get("run_status") == "harness_error" or row.get("harness_error"):
        return "harness_error", (row.get("harness_error") or message)[:200]
    if row.get("resolved"):
        return "resolved", f"f2p {row.get('f2p_passed')}"
    if details.startswith("Out of scope"):
        return "out_of_scope", details[:200]
    if "LLM error during identification" in log or message.startswith("LLM error"):
        return "identify_error", _evidence(log, "LLM error") or message[:200]
    if "ReAct final answer unparseable" in log:
        return "react_empty_final", _evidence(log, "ReAct final answer unparseable")
    if "answer named no task" in text:
        return "named_no_task", _evidence(log, "answer named no task")
    if status is None and "No improvements identified" in message:
        return "no_task_other", _evidence(log, "No improvements identified") or message[:200]
    if details.startswith("Planning call failed") or "implementation plan" in details:
        return "plan_failed", details[:200]
    if "TruncatedResponse" in details:
        return "generate_truncated", details[:200]
    if "EditMismatch" in details:
        return "edit_mismatch", details[:200]
    if details.startswith("Generation failed") or "Failed to generate code changes" in details:
        return "generate_empty", details[:200]
    if details.startswith("Reviewer rejection"):
        return "reviewer_rejected", details[:200]
    if status == "reverted":
        return "tests_regressed", details[:200]
    if row.get("false_merge"):
        return "false_merge", f"f2p {row.get('f2p_passed')}, files {row.get('changed_files')}"
    done, total = _f2p(row)
    if 0 < done < total:
        return "partial_fix", f"f2p {row.get('f2p_passed')}"
    if row.get("changed_files"):
        return "wrong_fix", f"f2p {row.get('f2p_passed')}, files {row.get('changed_files')}"
    return "other", f"cycle={status} {details[:150] or message[:150]}"


def habits(run_dir: Path) -> dict:
    """{signature: {"count", "meaning", "tasks": [{"id", "evidence"}]}}, most common first."""
    rows = json.loads((run_dir / "rows.json").read_text())
    table: dict = defaultdict(lambda: {"count": 0, "tasks": []})
    for row in rows:
        log_path = run_dir / f"{row['id']}.log"
        log = log_path.read_text(errors="replace") if log_path.exists() else ""
        sig, evidence = classify(row, log)
        note = request_note(log)
        if note and sig != "resolved":
            evidence = f"{evidence} [{note}]" if evidence else f"[{note}]"
        table[sig]["count"] += 1
        table[sig]["tasks"].append({"id": row["id"], "evidence": evidence})
    order = {s: i for i, (s, _) in enumerate(SIGNATURES)}
    return {sig: {"count": v["count"], "meaning": DESCRIPTIONS[sig], "tasks": v["tasks"]}
            for sig, v in sorted(table.items(), key=lambda kv: (-kv[1]["count"], order[kv[0]]))}


def top_habit(table: dict) -> str | None:
    """The most common miss, ignoring outcomes that aren't the pipeline's habit."""
    for sig in table:
        if sig not in ("resolved", "invalid", "harness_error"):
            return sig
    return None


def newest_run(mode: str = "cycle") -> Path:
    runs = sorted(d for d in common.RESULTS_DIR.glob(f"*-{mode}-*") if (d / "rows.json").exists())
    if not runs:
        raise SystemExit(f"no {mode} runs in {common.RESULTS_DIR}")
    return runs[-1]


def render(table: dict) -> str:
    lines = []
    for sig, v in table.items():
        lines.append(f"{v['count']:3}  {sig:20} {v['meaning']}")
        for t in v["tasks"][:3]:
            lines.append(f"       {t['id']}  {t['evidence']}")
        if len(v["tasks"]) > 3:
            lines.append(f"       ... and {len(v['tasks']) - 3} more")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_id", nargs="?", help="a directory under bench/results (default: newest cycle run)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    run_dir = common.RESULTS_DIR / args.run_id if args.run_id else newest_run()
    table = habits(run_dir)
    print(json.dumps(table, indent=2) if args.json else f"{run_dir.name}\n{render(table)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
