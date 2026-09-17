#!/usr/bin/env python3
"""Out-of-loop health audit for ouroboros.

Reads the agent's own record of itself and reports whether that record is
believable. Deliberately NOT part of src/ouroboros -- nothing in the improvement
cycle imports or calls this. A health signal the loop can invoke is a health
signal the loop can silence; this one only runs when something outside the loop
runs it.

Every check answers a question the system could not answer about itself on
2026-08-11, when it had been dead for 11 days while reporting 80% success.

Usage:
    python3 tools/health_audit.py [--repo PATH] [--json]

Exit codes:
    0  OK
    1  WARN
    2  CRITICAL   (suitable for a cron/systemd failure)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Thresholds. Deliberately conservative -- these are "something is wrong",
# not "tune me".
STALE_DAYS = 3         # no merged improvement in N days -> the loop is not working
STREAK_LIMIT = 3       # N consecutive failures for the same reason -> stuck
DRIFT_POINTS = 20.0    # reported vs actual success rate divergence, in points
REPEAT_LIMIT = 3       # same task description attempted N times -> no memory
HEARTBEAT_STALE_MIN = 90   # observed feed cadence is ~30 min; 3 missed = stale
UNRECORDED_HOURS = 6       # attempted but nothing written -> attempts are vanishing

OK, WARN, CRIT = "OK", "WARN", "CRITICAL"
RANK = {OK: 0, WARN: 1, CRIT: 2}


@dataclass
class Check:
    name: str
    status: str
    detail: str
    evidence: list[str] = field(default_factory=list)


def _days_ago(ts: float, now: float) -> float:
    return (now - ts) / 86400.0


def _load_history(repo: Path) -> list[dict]:
    p = repo / "config" / "improvement_history.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _load_learnings(repo: Path) -> list[dict]:
    """Parse `DATE | task_type | description | status | reason` lines."""
    p = repo / "config" / "learnings.md"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        parts = [f.strip() for f in line.split("|")]
        if len(parts) >= 4 and parts[0][:2].isdigit():
            rows.append({
                "date": parts[0],
                "task_type": parts[1],
                "description": parts[2],
                "status": parts[3],
                "reason": parts[4] if len(parts) > 4 else "",
            })
    return rows


def _load_state(repo: Path) -> dict:
    """config/state.json -- the loop's own runtime clock, committed every cycle.

    This is the only liveness evidence that travels with the repo. Probing the
    local host (ps/launchctl/cron) is the wrong instrument: the loop runs on one
    machine and this checkout may be a mirror, so a host probe reports "stopped"
    on a machine that legitimately has no scheduler while the loop cycles fine
    elsewhere. A confident false negative is worse than no check.
    """
    p = repo / "config" / "state.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _ts(state: dict, key: str) -> float | None:
    try:
        return float(state[key])
    except (KeyError, TypeError, ValueError):
        return None


def _load_metrics(repo: Path) -> list[dict]:
    p = repo / "config" / "metrics.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        return data.get("snapshots", [])
    return data if isinstance(data, list) else []


def _is_merged(entry: dict) -> bool:
    return entry.get("outcome") == "merged"


def _zero_test_delta(entry: dict) -> bool:
    """True when the post-change test run reported neither passes nor failures.

    This is the hollow-gate signature: pytest collected nothing, the runner
    returned 0/0, and the merge gate read "no regression".
    """
    after = (entry.get("test_delta") or {}).get("after") or {}
    return after.get("passed") == 0 and after.get("failed") == 0


# --- checks ----------------------------------------------------------------

def check_liveness(history: list[dict], now: float) -> Check:
    merged = [e for e in history if _is_merged(e) and e.get("timestamp")]
    if not merged:
        return Check("liveness", CRIT, "no merged improvement has ever been recorded")
    last = max(e["timestamp"] for e in merged)
    days = _days_ago(last, now)
    stamp = time.strftime("%Y-%m-%d", time.localtime(last))
    # "recorded" is load-bearing: this measures the autonomous loop's own
    # record. A human or an agent merging a PR by hand does not move it, and
    # should not -- see check_heartbeat/check_attempt_recording for the rest.
    detail = f"{days:.1f} days since last merged improvement recorded (last: {stamp})"
    status = CRIT if days > STALE_DAYS * 2 else WARN if days > STALE_DAYS else OK
    return Check("liveness", status, detail)


def check_heartbeat(state: dict, now: float) -> Check:
    """Is the process alive at all, on whatever host runs it?

    Distinguishes the two states a stale history cannot tell apart:
      heartbeat fresh + history stale  -> running, improvement cycle broken
      heartbeat stale + history stale  -> not running anywhere
    """
    last = _ts(state, "last_check")
    if last is None:
        return Check("heartbeat", CRIT, "state.json records no last_check; cannot tell if anything runs")
    mins = (now - last) / 60.0
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(last))
    detail = f"feed loop last cycled {mins:.0f} min ago ({stamp})"
    if mins > HEARTBEAT_STALE_MIN * 2:
        return Check("heartbeat", CRIT, detail + " -- nothing is running")
    if mins > HEARTBEAT_STALE_MIN:
        return Check("heartbeat", WARN, detail + " -- missed several cycles")
    return Check("heartbeat", OK, detail + " (local checkout may lag origin)")


def check_attempt_recording(state: dict, history: list[dict], now: float) -> Check:
    """Attempts that leave no record.

    last_improvement_attempt moves whenever the cycle is entered, including when
    it declines to run (open PR, deferred, error). improvement_history only gains
    an entry when an attempt actually executes. A wide gap between them means
    attempts are being declined or dying silently.

    The reason IS recorded -- improvement_runner writes it to
    ~/.config/moltbook/self_improvement_state.json -- but that path is outside
    the repo, so it never travels with the checkout and cannot be read from a
    mirror. git_ops._AUTO_STATE_FILES allowlists "config/self_improvement_state.json",
    a path nothing writes, so the one mechanism that would have carried the skip
    reason into git is pointed at a file that does not exist.
    """
    attempted = _ts(state, "last_improvement_attempt")
    if attempted is None:
        return Check("attempt-recording", WARN, "state.json records no last_improvement_attempt")
    newest = max((e.get("timestamp", 0) for e in history), default=0)
    gap_h = (attempted - newest) / 3600.0
    a_stamp = time.strftime("%m-%d %H:%M", time.localtime(attempted))
    r_stamp = time.strftime("%m-%d %H:%M", time.localtime(newest)) if newest else "never"
    if gap_h <= UNRECORDED_HOURS:
        return Check("attempt-recording", OK, f"last attempt {a_stamp}, last recorded {r_stamp}")
    return Check(
        "attempt-recording", CRIT,
        f"cycle entered {a_stamp} but nothing recorded since {r_stamp} ({gap_h:.0f}h gap)",
        ["attempts are being declined or dying before record_improvement()",
         "reason is on the running host at ~/.config/moltbook/self_improvement_state.json",
         "not readable here: git_ops allowlists config/self_improvement_state.json, which nothing writes"],
    )


def check_verifier_validity(history: list[dict]) -> Check:
    """Merges that landed while the test suite reported 0 passed / 0 failed.

    A zero-count test run is not a pass. It means the harness did not run.
    Anything merged on that signal was merged on nothing.
    """
    bad = [e for e in history if _is_merged(e) and _zero_test_delta(e)]
    if not bad:
        return Check("verifier-validity", OK, "no merges on an empty test result")
    recent = sorted(bad, key=lambda e: e.get("timestamp", 0))[-3:]
    evidence = [
        f"{time.strftime('%Y-%m-%d', time.localtime(e.get('timestamp', 0)))} "
        f"{e.get('task_type', '?')} -> {e.get('pr_url') or 'no PR'}"
        for e in recent
    ]
    return Check(
        "verifier-validity", CRIT,
        f"{len(bad)} merge(s) landed on a test result of 0 passed / 0 failed",
        evidence,
    )


def check_failure_streak(learnings: list[dict]) -> Check:
    """Consecutive trailing failures, and whether they share one reason.

    A loop that fails the same way N times in a row is not retrying, it is
    stuck. Nothing in the cycle notices, because each attempt is scored alone.
    """
    streak, reason = 0, None
    for row in reversed(learnings):
        if row["status"] != "failed":
            break
        if reason is None:
            reason = row["reason"]
        elif row["reason"] != reason:
            break
        streak += 1
    if streak == 0:
        return Check("failure-streak", OK, "most recent attempt did not fail")
    detail = f"{streak} consecutive failures, all '{reason or 'unspecified'}'"
    status = CRIT if streak >= STREAK_LIMIT * 2 else WARN if streak >= STREAK_LIMIT else OK
    return Check("failure-streak", status, detail)


def check_metrics_freshness(history: list[dict], metrics: list[dict], now: float) -> Check:
    """Is the published health signal newer than the events it claims to cover?"""
    if not metrics:
        return Check("metrics-freshness", CRIT, "no metrics snapshot has ever been written")
    newest_metric = max(s.get("timestamp", 0) for s in metrics)
    newest_event = max((e.get("timestamp", 0) for e in history), default=0)
    lag_days = _days_ago(newest_metric, now)
    stamp = time.strftime("%Y-%m-%d", time.localtime(newest_metric))
    detail = (
        f"newest snapshot {stamp} ({lag_days:.0f}d old), "
        f"{len(metrics)} snapshot(s) total"
    )
    if newest_metric < newest_event:
        behind = _days_ago(newest_metric, newest_event)
        return Check(
            "metrics-freshness", CRIT,
            detail + f" -- {behind:.0f}d behind the newest recorded attempt",
            ["record_snapshot() is likely never called from the improvement cycle"],
        )
    return Check("metrics-freshness", WARN if lag_days > STALE_DAYS else OK, detail)


def check_reported_vs_actual(history: list[dict], metrics: list[dict], now: float) -> Check:
    """The published success rate against one computed from the raw record."""
    if not metrics:
        return Check("reported-vs-actual", WARN, "nothing published to compare against")
    reported = metrics[-1].get("success_rate_30d")
    if reported is None:
        return Check("reported-vs-actual", WARN, "published snapshot carries no success rate")

    cutoff = now - 30 * 86400
    window = [e for e in history if e.get("timestamp", 0) >= cutoff]
    if not window:
        return Check(
            "reported-vs-actual", CRIT,
            f"dashboard claims {reported:.0f}% but no attempts recorded in the last 30 days",
        )
    actual = 100.0 * sum(1 for e in window if _is_merged(e)) / len(window)
    drift = abs(actual - reported)
    detail = (
        f"published {reported:.0f}% vs actual {actual:.0f}% "
        f"over {len(window)} attempts in 30d"
    )
    status = CRIT if drift > DRIFT_POINTS * 2 else WARN if drift > DRIFT_POINTS else OK
    return Check("reported-vs-actual", status, detail)


def check_repeat_work(history: list[dict]) -> Check:
    """The same task attempted repeatedly -- the loop has no memory of solving it."""
    counts = Counter(
        (e.get("description") or "")[:60].strip().lower()
        for e in history
        if e.get("description")
    )
    repeats = [(d, n) for d, n in counts.items() if n >= REPEAT_LIMIT]
    if not repeats:
        return Check("repeat-work", OK, "no task attempted more than twice")
    repeats.sort(key=lambda x: -x[1])
    evidence = [f"{n}x  {d[:64]}" for d, n in repeats[:3]]
    return Check(
        "repeat-work", WARN,
        f"{len(repeats)} task(s) attempted {REPEAT_LIMIT}+ times",
        evidence,
    )


def run_audit(repo: Path, now: float) -> list[Check]:
    history = _load_history(repo)
    learnings = _load_learnings(repo)
    metrics = _load_metrics(repo)

    if not history:
        return [Check("history", CRIT, f"no readable improvement history under {repo}/config")]

    state = _load_state(repo)
    return [
        check_heartbeat(state, now),
        check_attempt_recording(state, history, now),
        check_liveness(history, now),
        check_verifier_validity(history),
        check_failure_streak(learnings),
        check_metrics_freshness(history, metrics, now),
        check_reported_vs_actual(history, metrics, now),
        check_repeat_work(history),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    now = time.time()
    checks = run_audit(args.repo, now)
    worst = max((RANK[c.status] for c in checks), default=0)

    if args.json:
        print(json.dumps({
            "verdict": [k for k, v in RANK.items() if v == worst][0],
            "checked_at": now,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "evidence": c.evidence}
                for c in checks
            ],
        }, indent=2))
        return worst

    mark = {OK: "  ok  ", WARN: " warn ", CRIT: " CRIT "}
    print(f"\nouroboros health audit -- {args.repo}")
    print("=" * 72)
    for c in checks:
        print(f"[{mark[c.status]}] {c.name:20} {c.detail}")
        for line in c.evidence:
            print(f"{'':31}  - {line}")
    print("=" * 72)
    verdict = [k for k, v in RANK.items() if v == worst][0]
    print(f"verdict: {verdict}\n")
    return worst


if __name__ == "__main__":
    sys.exit(main())
