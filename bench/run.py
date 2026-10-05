#!/usr/bin/env python3
"""Run the improvement pipeline against the seeded-bug tasks and score it.

Modes:
  cycle  the real scheduled cycle (improvement_runner.run_scheduled_self_improvement),
         sandboxed: no remote, git/GitHub/Telegram side effects stubbed, isolated HOME
  gold   apply the historical fix (must score 100%: proves the tasks are solvable)
  null   change nothing (must score 0%: proves the tasks are not already solved)

Scoring never trusts the cycle's own verdict. Only the agent's change under
src/ouroboros/ is carried into a fresh, pristine snapshot, and F2P and P2P are
run there.
See bench/PLAN.md.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

TSV_HEADER = ("run_id\tdate\tmode\tagent_sha\tmodel_cfg\ttask_set\tsplit\tn\tscored\tresolved\t"
              "resolved_rate\tfalse_merge\tbroke_other\tllm_requests\tprompt_tokens\t"
              "completion_tokens\twall_min\tnote\n")
CYCLE_TIMEOUT = 45 * 60


# --------------------------------------------------------------------------- worker

def worker(args: argparse.Namespace) -> int:
    """Runs inside the snapshot (cwd), in its own process. Never call directly."""
    snap = Path.cwd()
    out = Path(args.worker_out)
    sys.path.insert(0, str(Path(args.agent_src).resolve()))
    import openai.resources.chat.completions as occ
    import ouroboros
    from ouroboros import git_ops, improvement_runner

    agent_pkg = Path(ouroboros.__file__).resolve().parent
    assert agent_pkg == (Path(args.agent_src).resolve() / "ouroboros"), agent_pkg

    # Everything the cycle spawns (pytest) must import the snapshot's code.
    os.environ["PYTHONPATH"] = str(snap / "src")
    probe = subprocess.run([sys.executable, "-c", "import ouroboros;print(ouroboros.__file__)"],
                           capture_output=True, text=True, cwd=snap).stdout.strip()
    assert Path(probe).resolve().is_relative_to(snap.resolve()), f"children import {probe}"

    # Fresh agent: nothing from the snapshot's own past may reach the prompts.
    from ouroboros.evaluation import load_history
    assert not load_history(snap), "snapshot carries improvement history"

    from ouroboros import llm
    from ouroboros.retry import is_daily_quota_exhausted

    record: dict = {"requests": 0, "quota_429": 0, "stubbed": [], "pr_opened": False}
    counter = Path(args.counter)
    stop_flag = counter.with_name("STOP")

    def _take_request() -> None:
        if stop_flag.exists():
            raise RuntimeError(f"bench request budget exhausted ({stop_flag.read_text().strip()})")
        with open(counter, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            used = int(f.read().strip() or 0)
            if used >= args.max_requests:
                raise RuntimeError("bench request budget exhausted (--max-requests)")
            f.seek(0)
            f.truncate()
            f.write(str(used + 1))
        record["requests"] += 1

    real_create = occ.Completions.create

    def counted_create(self, *a, **kw):
        _take_request()
        try:
            return real_create(self, *a, **kw)
        except Exception as exc:
            if "429" in str(exc):
                record["quota_429"] += 1
            if type(exc).__name__ in ("AuthenticationError", "PermissionDeniedError"):
                record["auth_error"] = True
            if is_daily_quota_exhausted(exc):
                # The key is shared with the live agent: stop the whole run
                # rather than keep spending what production has left.
                stop_flag.write_text("daily quota 429")
            raise

    occ.Completions.create = counted_create

    # The client holds the key once built; nothing the cycle spawns needs it.
    real_make_client = llm.make_runner_client

    def make_client_then_drop_key(cfg):
        client = real_make_client(cfg)
        os.environ.pop("LLM_API_KEY", None)
        return client

    llm.make_runner_client = make_client_then_drop_key

    def stub(module, name, value):
        def fn(*a, **kw):
            record["stubbed"].append(name)
            if name == "create_pr":
                record["pr_opened"] = True
            return value
        setattr(module, name, fn)

    for name, value in (("pull_latest", False), ("has_open_improvement_prs", False),
                        ("create_branch", None), ("commit_changes", None),
                        ("push_branch", None), ("checkout_branch", None),
                        ("create_pr", "bench://pr"), ("auto_merge_pr", False)):
        stub(git_ops, name, value)
    stub(improvement_runner, "check_pr_outcomes", None)
    stub(improvement_runner, "_send_notification", None)
    stub(improvement_runner, "_maybe_create_followup_issue", None)

    captured: dict = {}
    real_cycle = improvement_runner.run_improvement_cycle

    def capture_cycle(*a, **kw):
        res = real_cycle(*a, **kw)
        captured["result"] = res
        return res

    improvement_runner.run_improvement_cycle = capture_cycle

    t0 = time.time()
    try:
        run = improvement_runner.run_scheduled_self_improvement(force=True)
        record["run_status"], record["run_message"] = run.status, (run.message or "")[:2000]
    except Exception as exc:  # recorded, scored as unresolved
        record["run_status"], record["run_message"] = "harness_error", repr(exc)[:2000]
    record["seconds"] = round(time.time() - t0, 1)
    res = captured.get("result")
    if res is not None:
        record["cycle_status"] = res.status
        record["details"] = (res.details or "")[:2000]
        record["task"] = {"type": res.task.task_type, "description": res.task.description[:1000],
                          "target_files": res.task.target_files}
        record["usage"] = res.total_usage
        record["changed_files"] = [c.file_path for c in res.changes]
    out.write_text(json.dumps(record, indent=2))
    return 0


# --------------------------------------------------------------------------- parent

def load_tasks(split: str, only: list[str]) -> list[dict]:
    tasks = []
    for d in sorted(common.TASKS_DIR.iterdir()):
        if not (d / "task.json").exists():
            continue
        t = json.loads((d / "task.json").read_text())
        if only and t["id"] not in only:
            continue
        if not only and split != "all" and t["split"] != split:
            continue
        t["dir"] = str(d)
        tasks.append(t)
    return tasks


def task_set_hash(tasks: list[dict]) -> str:
    h = hashlib.sha256()
    for t in tasks:
        h.update((Path(t["dir"]) / "task.json").read_bytes())
        h.update((Path(t["dir"]) / "test.patch").read_bytes())
    return h.hexdigest()[:10]


def bench_agent_json(home: Path) -> dict:
    """Production's committed runtime config, made safe for the sandbox."""
    cfg = json.loads((common.REPO / "config" / "agent.json").read_text())
    cfg["enable_auto_merge"] = False
    # No overflow gateway: a quota-limited bench run must fail loudly, not
    # silently switch to a different model and score that instead.
    cfg["llm_overflow_base_url"] = ""
    cfg["llm_overflow_model"] = ""
    target = home / ".config" / "moltbook" / "agent.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cfg, indent=2))
    return cfg


def model_cfg_hash(cfg: dict) -> str:
    keys = sorted(k for k in cfg if any(s in k for s in ("model", "backend", "base_url", "effort")))
    blob = json.dumps({k: cfg[k] for k in keys}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


SANDBOX_PROFILE = """(version 1)
(allow default)
(deny file-write*)
(allow file-write* (subpath "{work}") (subpath "{shared}") (subpath "/private/var/folders")
       (subpath "/dev"))
(deny file-read* (subpath "{home}"))
(allow file-read* (subpath "{home}/.local/share/uv") (subpath "{home}/.cache/uv"))
(deny file-read* (subpath "{tasks}") (subpath "{results}"))
"""


def sandbox_cmd(cmd: list[str], work: Path, shared: Path) -> list[str]:
    """Jail the worker and everything it spawns, generated code included.

    Writes only into its own sandbox and the run's shared counter dir; no
    reads of the user's home (keys, ssh, other repos) or of the gold patches.
    Network stays open because the worker must reach the LLM gateway; the key
    is dropped from the environment once the client holds it.
    """
    profile = SANDBOX_PROFILE.format(
        work=work.resolve(), shared=shared.resolve(), home=Path.home().resolve(),
        tasks=common.TASKS_DIR.resolve(), results=common.RESULTS_DIR.resolve())
    return ["sandbox-exec", "-p", profile, *cmd]


def agent_src_patch(snap: Path) -> str:
    """The agent's change, limited to production code.

    Scoring applies only this to a pristine snapshot, so nothing the agent did
    to tests, conftest, pytest config or anything else can reach the score.
    """
    subprocess.run(["git", "add", "-A", "--", "src/ouroboros"], cwd=snap, capture_output=True)
    return subprocess.run(["git", "diff", "--cached", "--binary", "HEAD", "--", "src/ouroboros"],
                          cwd=snap, capture_output=True, text=True).stdout


def run_task(task: dict, mode: str, run_dir: Path, args: argparse.Namespace) -> dict:
    tdir = Path(task["dir"])
    work = Path(tempfile.mkdtemp(prefix=f"bench-{task['id']}-"))
    snap = work / "snap"
    row = {"id": task["id"], "mode": mode}
    try:
        common.materialize(common.REPO, task["parent"], snap,
                           (tdir / "test.patch").read_text(), init_git=True)

        if mode == "gold":
            common.apply_patch(snap, (tdir / "gold.patch").read_text())
        elif mode == "cycle":
            home = work / "home"
            (home / ".config" / "moltbook").mkdir(parents=True, exist_ok=True)
            bench_agent_json(home)
            fakebin = work / "bin"
            fakebin.mkdir()
            gh = fakebin / "gh"
            gh.write_text("#!/bin/sh\necho 'gh disabled in bench sandbox' >&2\nexit 1\n")
            gh.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if k not in common.SECRET_ENV and k != "PYTHONPATH"}
            env.update(HOME=str(home), PATH=f"{fakebin}:{env.get('PATH', '')}",
                       LLM_API_KEY=os.environ["BENCH_LLM_API_KEY"], PYTHONDONTWRITEBYTECODE="1")
            wout = work / "worker.json"
            cmd = [sys.executable, str(Path(__file__).resolve()), "--worker",
                   "--worker-out", str(wout), "--agent-src", str(args.agent_src),
                   "--counter", str(args.shared / "requests.count"),
                   "--max-requests", str(args.max_requests)]
            with open(run_dir / f"{task['id']}.log", "w") as log:
                try:
                    subprocess.run(sandbox_cmd(cmd, work, args.shared), cwd=snap, env=env,
                                   stdout=log, stderr=subprocess.STDOUT, timeout=CYCLE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    row["worker_timeout"] = True
            row.update(json.loads(wout.read_text()) if wout.exists() else {"run_status": "no_worker_output"})

        full_diff = subprocess.run(["git", "diff", "HEAD"], cwd=snap, capture_output=True, text=True).stdout
        (run_dir / f"{task['id']}.diff").write_text(full_diff)
        src_patch = agent_src_patch(snap)

        # Score in a pristine snapshot that receives only the production-code
        # change. Its baseline is measured there too: a snapshot with .git and
        # one without do not run every test the same way.
        score = work / "score"
        common.materialize(common.REPO, task["parent"], score, (tdir / "test.patch").read_text())
        base_full = common.run_pytest(score, [], timeout=300)
        common.apply_patch(score, src_patch)
        snap = score

        targeted = common.run_pytest(snap, task["test_files"], timeout=300)
        got = targeted["outcomes"]
        f2p_ok = [t for t in task["f2p"] if got.get(t) == "passed"]
        p2p_bad = [t for t in task["p2p"] if got.get(t) != "passed"]
        row["f2p_passed"] = f"{len(f2p_ok)}/{len(task['f2p'])}"
        row["p2p_broken"] = len(p2p_bad)
        row["resolved"] = len(f2p_ok) == len(task["f2p"]) and not p2p_bad

        after_full = common.run_pytest(snap, [], timeout=300)
        in_task = tuple(task["test_files"])
        before_ok = {t for t, o in base_full["outcomes"].items() if o == "passed" and not t.startswith(in_task)}
        after_ok = {t for t, o in after_full["outcomes"].items() if o == "passed"}
        row["broke_other"] = len(before_ok - after_ok)
        # The cycle would have opened a PR (and auto-merged it in production).
        row["would_merge"] = row.get("cycle_status") == "success"
        row["false_merge"] = row["would_merge"] and not row["resolved"]
        # Not the pipeline's fault: the budget or the gateway ran out under it.
        msg = str(row.get("run_message", "")) + str(row.get("details", ""))
        row["invalid"] = ("budget exhausted" in msg or bool(row.get("auth_error"))
                          or (row.get("quota_429", 0) > 0 and not row["resolved"]))
        return row
    except Exception as exc:
        row.update(resolved=False, invalid=True, harness_error=repr(exc)[:500])
        return row
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            row["workdir"] = str(work)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=("cycle", "gold", "null"), default="cycle")
    ap.add_argument("--split", choices=("dev", "heldout", "all"), default="dev")
    ap.add_argument("--task", action="append", default=[], help="run only these task ids")
    ap.add_argument("--workers", type=int, default=0,
                    help="parallel tasks (default: 2 for cycle, which shares a rate-limited key; 6 otherwise)")
    ap.add_argument("--max-requests", type=int, default=300)
    ap.add_argument("--agent-src", type=Path, default=common.REPO / "src",
                    help="the ouroboros package under test")
    ap.add_argument("--note", default="")
    ap.add_argument("--keep", action="store_true", help="keep sandboxes for inspection")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--worker-out", help=argparse.SUPPRESS)
    ap.add_argument("--counter", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.worker:
        return worker(args)
    if args.mode == "cycle" and not os.environ.get("BENCH_LLM_API_KEY"):
        print("BENCH_LLM_API_KEY is not set (the OpenRouter key production uses)", file=sys.stderr)
        return 2

    args.workers = args.workers or (2 if args.mode == "cycle" else 6)
    # Outside bench/results, which the sandbox cannot read.
    args.shared = Path(tempfile.mkdtemp(prefix="bench-shared-"))
    tasks = load_tasks(args.split, args.task)
    if not tasks:
        print("no tasks", file=sys.stderr)
        return 2
    agent_root = args.agent_src.resolve().parent
    agent_sha = common.git(agent_root, "rev-parse", "--short", "HEAD").strip()
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{args.mode}-{uuid.uuid4().hex[:4]}"
    run_dir = common.RESULTS_DIR / run_id
    run_dir.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = bench_agent_json(Path(tmp))

    print(f"{run_id}: {len(tasks)} tasks, mode={args.mode}, agent={agent_sha}", flush=True)
    t0 = time.time()

    def one(t: dict) -> dict:
        row = run_task(t, args.mode, run_dir, args)
        flag = "RESOLVED" if row.get("resolved") else ("invalid " if row.get("invalid") else "unresolved")
        print(f"  {flag}  {t['id']}  f2p={row.get('f2p_passed', '?')}  "
              f"cycle={row.get('cycle_status', row.get('run_status', '-'))}  "
              f"req={row.get('requests', 0)}", flush=True)
        return row

    with ThreadPoolExecutor(args.workers) as pool:
        rows = list(pool.map(one, tasks))

    (run_dir / "rows.json").write_text(json.dumps(rows, indent=2))
    scored = [r for r in rows if not r.get("invalid")]
    resolved = sum(bool(r.get("resolved")) for r in scored)
    usage = [r.get("usage") or {} for r in rows]
    summary = {
        "run_id": run_id, "date": time.strftime("%Y-%m-%d"), "mode": args.mode,
        "agent_sha": agent_sha, "model_cfg": model_cfg_hash(cfg) if args.mode == "cycle" else "-",
        "task_set": task_set_hash(tasks), "split": args.split if not args.task else "custom",
        "n": len(rows), "scored": len(scored), "resolved": resolved,
        "resolved_rate": round(resolved / len(scored), 3) if scored else 0.0,
        "false_merge": sum(bool(r.get("false_merge")) for r in scored),
        "broke_other": sum(1 for r in scored if r.get("broke_other")),
        "llm_requests": sum(r.get("requests", 0) for r in rows),
        "prompt_tokens": sum(u.get("prompt_tokens", 0) for u in usage),
        "completion_tokens": sum(u.get("completion_tokens", 0) for u in usage),
        "wall_min": round((time.time() - t0) / 60, 1), "note": args.note,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    tsv = common.BENCH / "results.tsv"
    new = not tsv.exists()
    with open(tsv, "a") as f:
        if new:
            f.write(TSV_HEADER)
        f.write("\t".join(str(summary[k]) for k in TSV_HEADER.strip().split("\t")) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
