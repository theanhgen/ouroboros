"""Known-answer tests for tools/health_audit.py.

The audit is a verifier. An unverified verifier is the exact defect it exists to
catch -- ouroboros merged six changes on a test result of 0 passed / 0 failed
because nothing checked whether the checker had actually run. So every check
here is pinned to a hand-built fixture with a known answer, and the clock is
injected rather than read.

Testing the audit does not make it in-loop: nothing in src/ouroboros imports it,
and that is the property that keeps it honest.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# tools/ is not a package -- load by path. Registering in sys.modules before
# exec is required: @dataclass resolves annotations via sys.modules[__module__].
_MODULE_PATH = Path(__file__).resolve().parent.parent / "tools" / "health_audit.py"
_spec = importlib.util.spec_from_file_location("health_audit", _MODULE_PATH)
ha = importlib.util.module_from_spec(_spec)
sys.modules["health_audit"] = ha
_spec.loader.exec_module(ha)

DAY = 86400.0
NOW = 1_786_400_000.0  # fixed clock; never time.time()


def entry(*, ts_days_ago=0.0, outcome="merged", passed=10, failed=0,
          description="do a thing", task_type="fix_bug", pr="http://pr/1"):
    """One improvement_history record. passed=failed=None omits the delta."""
    e = {
        "task_id": "t",
        "task_type": task_type,
        "description": description,
        "outcome": outcome,
        "pr_url": pr,
        "timestamp": NOW - ts_days_ago * DAY,
    }
    if passed is not None:
        e["test_delta"] = {"before": {"passed": 0, "failed": 0},
                           "after": {"passed": passed, "failed": failed}}
    return e


def learning(status="failed", reason="no plan generated", date="2026-08-09"):
    return {"date": date, "task_type": "fix_bug", "description": "d",
            "status": status, "reason": reason}


# --- the hollow-gate signature ---------------------------------------------

def test_zero_delta_detects_empty_test_run():
    assert ha._zero_test_delta(entry(passed=0, failed=0)) is True


def test_zero_delta_ignores_a_real_test_run():
    assert ha._zero_test_delta(entry(passed=249, failed=0)) is False


def test_zero_delta_does_not_fire_when_no_delta_was_recorded():
    """Absent is not the same as empty. Only an actual 0/0 reading is hollow."""
    assert ha._zero_test_delta(entry(passed=None)) is False


def test_verifier_validity_flags_merges_on_an_empty_test_run():
    c = ha.check_verifier_validity([entry(passed=0, failed=0), entry(passed=5)])
    assert c.status == ha.CRIT
    assert "1 merge" in c.detail


def test_verifier_validity_ignores_failed_attempts_with_no_tests():
    """A failed attempt that never reached tests is not a hollow merge."""
    c = ha.check_verifier_validity([entry(outcome="failed", passed=0, failed=0)])
    assert c.status == ha.OK


def test_verifier_validity_reports_at_most_three_most_recent():
    hist = [entry(ts_days_ago=d, passed=0, failed=0) for d in (50, 40, 30, 20, 10)]
    c = ha.check_verifier_validity(hist)
    assert "5 merge" in c.detail
    assert len(c.evidence) == 3


# --- liveness ---------------------------------------------------------------

@pytest.mark.parametrize("days,expected", [
    (1.0, ha.OK),      # inside one cycle
    (4.0, ha.WARN),    # > STALE_DAYS
    (11.6, ha.CRIT),   # the real 2026-08-11 reading
])
def test_liveness_thresholds(days, expected):
    assert ha.check_liveness([entry(ts_days_ago=days)], NOW).status == expected


def test_liveness_ignores_failed_attempts():
    """Recent activity is not recent success. Only merges move this."""
    hist = [entry(ts_days_ago=20), entry(ts_days_ago=0.5, outcome="failed")]
    assert ha.check_liveness(hist, NOW).status == ha.CRIT


def test_liveness_when_nothing_ever_merged():
    assert ha.check_liveness([entry(outcome="failed")], NOW).status == ha.CRIT


# --- failure streak ---------------------------------------------------------

def test_streak_counts_only_trailing_failures_of_one_reason():
    rows = [learning(status="success", reason="")] + [learning()] * 3
    c = ha.check_failure_streak(rows)
    assert c.status == ha.WARN
    assert "3 consecutive" in c.detail


def test_streak_escalates_to_critical():
    c = ha.check_failure_streak([learning()] * 6)
    assert c.status == ha.CRIT


def test_streak_breaks_on_a_different_reason():
    """Six failures, but not the same wall -- only the trailing run counts."""
    rows = [learning(reason="reviewer rejected")] * 3 + [learning(reason="no plan generated")] * 2
    assert "2 consecutive" in ha.check_failure_streak(rows).detail


def test_streak_clear_after_a_success():
    assert ha.check_failure_streak([learning()] * 5 + [learning(status="success")]).status == ha.OK


# --- published vs actual ----------------------------------------------------

def test_metrics_freshness_flags_a_snapshot_older_than_the_newest_event():
    hist = [entry(ts_days_ago=1)]
    metrics = [{"timestamp": NOW - 140 * DAY, "success_rate_30d": 80.0}]
    c = ha.check_metrics_freshness(hist, metrics, NOW)
    assert c.status == ha.CRIT
    assert "behind" in c.detail


def test_metrics_freshness_when_none_were_ever_written():
    assert ha.check_metrics_freshness([entry()], [], NOW).status == ha.CRIT


def test_reported_vs_actual_flags_the_real_divergence():
    """80% published against 35% actual -- the 2026-08-11 reading."""
    hist = [entry(ts_days_ago=d, outcome="merged" if d % 3 == 0 else "failed")
            for d in range(20)]
    c = ha.check_reported_vs_actual(hist, [{"timestamp": NOW, "success_rate_30d": 80.0}], NOW)
    assert c.status == ha.CRIT


def test_reported_vs_actual_accepts_an_honest_dashboard():
    hist = [entry(ts_days_ago=d) for d in range(10)]  # all merged -> 100%
    c = ha.check_reported_vs_actual(hist, [{"timestamp": NOW, "success_rate_30d": 100.0}], NOW)
    assert c.status == ha.OK


def test_reported_vs_actual_flags_a_claim_with_no_attempts_behind_it():
    hist = [entry(ts_days_ago=200)]
    c = ha.check_reported_vs_actual(hist, [{"timestamp": NOW, "success_rate_30d": 80.0}], NOW)
    assert c.status == ha.CRIT
    assert "no attempts" in c.detail


# --- memory -----------------------------------------------------------------

def test_repeat_work_flags_a_task_attempted_three_times():
    hist = [entry(description="fix git status --porcelain parsing")] * 3
    c = ha.check_repeat_work(hist)
    assert c.status == ha.WARN
    assert len(c.evidence) == 1


def test_repeat_work_tolerates_a_second_attempt():
    assert ha.check_repeat_work([entry(description="x")] * 2).status == ha.OK


# --- heartbeat: is anything running, on any host -----------------------------

def test_heartbeat_ok_when_the_loop_cycled_recently():
    c = ha.check_heartbeat({"last_check": NOW - 20 * 60}, NOW)
    assert c.status == ha.OK


def test_heartbeat_warns_after_several_missed_cycles():
    c = ha.check_heartbeat({"last_check": NOW - 120 * 60}, NOW)
    assert c.status == ha.WARN


def test_heartbeat_critical_when_nothing_has_cycled():
    c = ha.check_heartbeat({"last_check": NOW - 3 * DAY}, NOW)
    assert c.status == ha.CRIT
    assert "nothing is running" in c.detail


def test_heartbeat_critical_when_state_records_no_clock():
    assert ha.check_heartbeat({}, NOW).status == ha.CRIT


def test_heartbeat_survives_a_junk_timestamp():
    assert ha.check_heartbeat({"last_check": "not-a-number"}, NOW).status == ha.CRIT


def test_heartbeat_is_host_independent():
    """The signal comes from committed state, never from probing this machine.

    A host probe reports 'stopped' on a mirror checkout while the loop cycles on
    another host. That false negative is why this check reads state.json instead.
    """
    fresh = ha.check_heartbeat({"last_check": NOW - 60}, NOW)
    assert fresh.status == ha.OK
    assert "lag origin" in fresh.detail


# --- attempts that leave no record -------------------------------------------

def test_attempt_recording_flags_the_gap_between_entered_and_recorded():
    """The 2026-08-11 reading: cycle entered 8h ago, nothing recorded for 54h."""
    state = {"last_improvement_attempt": NOW - 8 * 3600}
    c = ha.check_attempt_recording(state, [entry(ts_days_ago=2.25)], NOW)
    assert c.status == ha.CRIT
    assert "gap" in c.detail


def test_attempt_recording_ok_when_the_attempt_was_recorded():
    state = {"last_improvement_attempt": NOW - 3600}
    c = ha.check_attempt_recording(state, [entry(ts_days_ago=1.0 / 24)], NOW)
    assert c.status == ha.OK


def test_attempt_recording_ok_when_recording_leads_the_attempt():
    """A record newer than the attempt clock is fine -- never a negative gap."""
    state = {"last_improvement_attempt": NOW - 5 * 3600}
    c = ha.check_attempt_recording(state, [entry(ts_days_ago=0)], NOW)
    assert c.status == ha.OK


def test_attempt_recording_warns_when_the_clock_is_absent():
    assert ha.check_attempt_recording({}, [entry()], NOW).status == ha.WARN


def test_attempt_recording_handles_an_empty_history():
    state = {"last_improvement_attempt": NOW}
    assert ha.check_attempt_recording(state, [], NOW).status == ha.CRIT


# --- parsing and rollup -----------------------------------------------------

def test_learnings_parser_handles_the_real_line_format(tmp_path):
    repo = _write_repo(tmp_path, learnings=(
        "2026-03-30 | fix_bug |  | failed | reviewer rejected\n"
        "2026-07-31 | fix_bug | Fix _parse_pytest_output in test_runner | success | tests: 247 -> 249\n"
        "not a learnings line at all\n"
    ))
    rows = ha._load_learnings(repo)
    assert len(rows) == 2
    assert rows[0]["reason"] == "reviewer rejected"
    assert rows[1]["status"] == "success"


def _write_repo(root: Path, history=None, metrics=None, learnings="", state=None):
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "improvement_history.json").write_text(json.dumps(history or []))
    (root / "config" / "metrics.json").write_text(json.dumps({"snapshots": metrics or []}))
    (root / "config" / "learnings.md").write_text(learnings)
    (root / "config" / "state.json").write_text(json.dumps(
        state if state is not None
        else {"last_check": NOW - 300, "last_improvement_attempt": NOW - 3600}))
    return root


def test_run_audit_on_a_healthy_repo_is_clean(tmp_path):
    repo = _write_repo(
        tmp_path,
        history=[entry(ts_days_ago=d, description=f"distinct task {d}") for d in range(3)],
        metrics=[{"timestamp": NOW, "success_rate_30d": 100.0}],
        learnings="2026-08-10 | fix_bug | d | success | tests: 1 -> 2\n",
    )
    
    checks = ha.run_audit(repo, NOW)
    assert all(c.status == ha.OK for c in checks), [(c.name, c.detail) for c in checks]


def test_run_audit_reports_critical_when_history_is_unreadable(tmp_path):
    checks = ha.run_audit(tmp_path, NOW)
    assert len(checks) == 1 and checks[0].status == ha.CRIT
