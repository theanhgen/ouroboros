"""Shared helpers for the bench harness: git, snapshots, targeted pytest runs."""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parent
TASKS_DIR = BENCH / "tasks"
RESULTS_DIR = BENCH / "results"
# Every request the bench spends on the OpenRouter key, by day, across runs and
# outer-loop invocations. The key is production's: the bench gets what is left
# under DAILY_CAP, and a 429 must never be the first sign the day is spent.
LEDGER = RESULTS_DIR / "ledger.tsv"
DAILY_CAP = int(os.environ.get("BENCH_DAILY_CAP", "300"))

# Agent state that a historical snapshot carries and a fresh agent must not
# see: it holds the agent's own notes about the very fix being benchmarked.
STATE_PATHS = (
    "config/ouroboros.db", "config/memory.db", "config/learnings.md",
    "config/backlog.json", "config/state.json", "config/metrics.json",
    "config/improvement_history.json", "config/knowledge_base.json",
    ".ouroboros", "docs/wiki", "MERGED", "ISSUES.md",
)

# Never handed to a snapshot's test run: tests mock the network, and a test
# that does not would spend the live agent's shared quota.
SECRET_ENV = ("OPENAI_API_KEY", "LLM_API_KEY", "LLM_OVERFLOW_API_KEY",
              "OPENROUTER_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "BW_SESSION")

_SUMMARY_RE = re.compile(r"^(PASSED|FAILED|ERROR|XPASS|XFAIL|SKIPPED)\s+(\S+)")


class PatchError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True).returncode == 0


def forbidden_paths() -> tuple[str, ...]:
    """The agent's immutable files, read from the harness checkout's config."""
    src = str(REPO / "src")
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]);"
         "from ouroboros.config import SafetyConfig;"
         "print('\\n'.join(SafetyConfig().forbidden_modification_paths))", src],
        check=True, capture_output=True, text=True, env=_clean_env(),
    ).stdout
    return tuple(line for line in out.splitlines() if line)


def apply_patch(root: Path, patch: str) -> None:
    if not patch.strip():
        return
    proc = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=root,
                          input=patch, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PatchError(proc.stderr.strip()[:300])


def materialize(repo: Path, rev: str, dest: Path, test_patch: str = "",
                strip_state: bool = True, init_git: bool = False) -> None:
    """Write rev's tree to dest with no history, then apply the test patch.

    git archive, not a checkout: the snapshot must not carry .git, or the
    agent could read the fix out of the future commits.
    """
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "-C", str(repo), "archive", rev],
                             check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    apply_patch(dest, test_patch)
    if strip_state:
        for rel in STATE_PATHS:
            p = dest / rel
            if p.is_dir():
                subprocess.run(["rm", "-rf", str(p)], check=True)
            elif p.exists():
                p.unlink()
    if init_git:
        env = {**_clean_env(), "GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "bench@localhost",
               "GIT_COMMITTER_NAME": "bench", "GIT_COMMITTER_EMAIL": "bench@localhost"}
        for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                     ["commit", "-q", "--no-verify", "-m", "snapshot"]):
            subprocess.run(["git", *args], cwd=dest, check=True, capture_output=True, env=env)


def _clean_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in SECRET_ENV and k != "PYTHONPATH" and not k.startswith("PYTEST_")}
    env.update(extra)
    return env


def snapshot_env(snap: Path) -> dict:
    """Environment for anything that imports the snapshot's own ouroboros."""
    home = snap.parent / "home"
    home.mkdir(exist_ok=True)
    return _clean_env(PYTHONPATH=str(snap / "src"), HOME=str(home),
                      PYTHONDONTWRITEBYTECODE="1")


def run_pytest(snap: Path, test_paths: list[str], timeout: int) -> dict:
    """Run pytest in a snapshot and return {outcomes: {nodeid: outcome}, ...}."""
    cmd = [sys.executable, "-m", "pytest", "-q", "-rA", "--no-header", "--tb=no",
           "-p", "no:cacheprovider", "-o", "addopts=", *test_paths]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=snap, env=snapshot_env(snap), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"outcomes": {}, "seconds": time.time() - t0, "timed_out": True, "stdout": ""}
    outcomes: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        m = _SUMMARY_RE.match(line)
        if m:
            outcomes[m.group(2)] = m.group(1).lower()
    return {"outcomes": outcomes, "seconds": time.time() - t0, "timed_out": False,
            "stdout": proc.stdout[-4000:], "returncode": proc.returncode}


def ledger_used(day: str | None = None) -> int:
    """Requests the bench has spent today (local date)."""
    day = day or time.strftime("%Y-%m-%d")
    if not LEDGER.exists():
        return 0
    return sum(int(line.split("\t")[2]) for line in LEDGER.read_text().splitlines()
               if line.startswith(day + "\t"))


def ledger_add(source: str, requests: int) -> None:
    if requests <= 0:
        return
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(f"{time.strftime('%Y-%m-%d')}\t{source}\t{requests}\n")


def daily_left() -> int:
    return max(0, DAILY_CAP - ledger_used())
