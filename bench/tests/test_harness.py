"""Harness tests. Run explicitly: python -m pytest bench/tests (not in the main testpaths)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_tasks  # noqa: E402
import common  # noqa: E402
import run  # noqa: E402

TASK = common.TASKS_DIR / "2026-09-25-3cc39930"


@pytest.fixture
def task():
    if not TASK.exists():
        pytest.skip("task set not built")
    return json.loads((TASK / "task.json").read_text())


def test_candidates_are_single_parent_fixes_touching_src_and_tests():
    cands = build_tasks.candidates(common.REPO)
    assert cands
    for c in cands:
        assert len(c["parent"].split()) == 1
        assert c["src"] and c["tests"]
        assert build_tasks.SUBJECT_RE.search(c["subject"])


def test_snapshot_has_no_history_and_no_agent_state(tmp_path, task):
    snap = tmp_path / "snap"
    common.materialize(common.REPO, task["parent"], snap, (TASK / "test.patch").read_text(),
                       init_git=True)
    for rel in common.STATE_PATHS:
        assert not (snap / rel).exists(), rel
    log = subprocess.run(["git", "log", "--oneline"], cwd=snap, capture_output=True, text=True).stdout
    assert len(log.splitlines()) == 1
    remotes = subprocess.run(["git", "remote"], cwd=snap, capture_output=True, text=True).stdout
    assert remotes.strip() == ""


def test_score_patch_carries_only_production_code(tmp_path, task):
    snap = tmp_path / "snap"
    common.materialize(common.REPO, task["parent"], snap, (TASK / "test.patch").read_text(),
                       init_git=True)
    (snap / "tests" / "conftest.py").write_text("def pytest_collection_modifyitems(items): pass\n")
    (snap / "conftest.py").write_text("# planted\n")
    (snap / "src" / "ouroboros" / "bench_marker.py").write_text("X = 1\n")
    patch = run.agent_src_patch(snap)
    assert "src/ouroboros/bench_marker.py" in patch
    assert "conftest" not in patch


def test_null_change_resolves_nothing_and_gold_resolves_all(tmp_path, task):
    snap = tmp_path / "snap"
    common.materialize(common.REPO, task["parent"], snap, (TASK / "test.patch").read_text())
    before = common.run_pytest(snap, task["test_files"], timeout=120)["outcomes"]
    assert not all(before.get(t) == "passed" for t in task["f2p"])
    common.apply_patch(snap, (TASK / "gold.patch").read_text())
    after = common.run_pytest(snap, task["test_files"], timeout=120)["outcomes"]
    assert all(after.get(t) == "passed" for t in task["f2p"] + task["p2p"])


def test_sandbox_blocks_gold_patches_and_home(tmp_path):
    work, shared = tmp_path / "work", tmp_path / "shared"
    work.mkdir()
    shared.mkdir()
    probe = ("import os,sys\n"
             "for p in sys.argv[1:]:\n"
             "    try: os.listdir(p); print('ALLOWED', p)\n"
             "    except PermissionError: print('blocked', p)\n")
    cmd = run.sandbox_cmd([sys.executable, "-c", probe, str(common.TASKS_DIR),
                           str(Path.home() / ".ssh"), str(work)], work, shared)
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    assert f"blocked {common.TASKS_DIR}" in out
    assert f"blocked {Path.home() / '.ssh'}" in out
    assert f"ALLOWED {work}" in out
