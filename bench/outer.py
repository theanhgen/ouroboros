"""The outer loop: edit program.md for the top habit, keep the edit only if the score rises.

One step per invocation, all on the OpenRouter key and inside the bench's
daily cap (common.DAILY_CAP, shared with every run.py run):

  python bench/outer.py                     # screen one new edit on smoke
  python bench/outer.py --confirm <exp-id>  # one paired replicate on confirm
  python bench/outer.py --heldout <exp-id>  # heldout check of a kept edit [--open-pr]

Screening only filters: a candidate that beats main's mean on the 10 smoke
tasks by a task goes on to confirmation. Keeping is decided on the other 19 dev
tasks, with fresh main and candidate runs side by side (order alternating), up
to MAX_REPLICATES days, by an exact one-sided McNemar test on the
(task, replicate) pairs where the two disagree. A kept edit still needs a
heldout check before --open-pr will open a PR.

Every step is a row in bench/experiments.tsv, so the next edit can see what was
already tried. The editor model gets no tools and never sees bench/tasks: only
program.md, the code that sends it, failure signatures with scrubbed log lines,
and past experiments.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import habits  # noqa: E402
import run as bench_run  # noqa: E402

OPENROUTER = "https://openrouter.ai/api/v1"
EDITOR_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
PROGRAM_REL = "src/ouroboros/program.md"
EXPERIMENTS = common.BENCH / "experiments.tsv"
EXP_FIELDS = ("exp_id", "date", "step", "base_sha", "cand_sha", "branch", "editor_model", "habit",
              "hypothesis", "split", "main", "cand", "runs", "wins", "losses", "p", "harm",
              "decision", "requests", "note")
SECTION_RE = re.compile(r"<!-- section: (\w+) -->\n(.*?)\n<!-- end: \1 -->", re.S)
MIN_MAIN_RUNS = 2       # cached main smoke runs a screen compares against
MAX_REPLICATES = 3      # paired confirm replicates before giving up
KEEP_P = 0.1            # one-sided McNemar p-value to keep
# Requests a step needs to finish (observed ~7 per task; confirm and heldout
# run main and candidate). A step that can't is not started: a run cut short
# is thrown away, so starting it would waste what it spent.
NEED = {"smoke": 100, "confirm": 280, "heldout": 210}


class BudgetExhausted(RuntimeError):
    pass


# --------------------------------------------------------------------------- bookkeeping

def tsv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    head = lines[0].split("\t")
    return [dict(zip(head, line.split("\t"))) for line in lines[1:] if line.strip()]


def append_experiment(row: dict) -> None:
    new = not EXPERIMENTS.exists()
    with open(EXPERIMENTS, "a") as f:
        if new:
            f.write("\t".join(EXP_FIELDS) + "\n")
        f.write("\t".join(str(row.get(k, "")).replace("\t", " ").replace("\n", " ")
                          for k in EXP_FIELDS) + "\n")


def history(exp_id: str) -> list[dict]:
    return [e for e in tsv_rows(EXPERIMENTS) if e["exp_id"] == exp_id]


def git(*args: str, cwd: Path = common.REPO) -> str:
    return common.git(cwd, *args)


def agent_tree(rev: str) -> str | None:
    """The agent's code at rev. Runs are matched on this, not the commit, so a
    commit that touches only bench/ or docs doesn't throw away main's runs."""
    proc = subprocess.run(["git", "-C", str(common.REPO), "rev-parse", f"{rev}:src/ouroboros"],
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def run_ref(split: str) -> dict:
    """What a results.tsv row must match to be comparable: tasks and models."""
    tasks = bench_run.load_tasks(split, [])
    with tempfile.TemporaryDirectory() as tmp:
        cfg = bench_run.bench_agent_json(Path(tmp))
    return {"task_set": bench_run.task_set_hash(tasks), "model_cfg": bench_run.model_cfg_hash(cfg),
            "split": split}


def with_outcomes(row: dict) -> dict:
    rows = json.loads((common.RESULTS_DIR / row["run_id"] / "rows.json").read_text())
    return {**row, "resolved_ids": {x["id"] for x in rows if x.get("resolved")}}


def complete(row: dict) -> bool:
    """Every task scored: a run cut short by budget or quota is not a sample."""
    return row["n"] == row["scored"] and int(row["n"]) > 0


def scored_runs(tree: str, ref: dict) -> list[dict]:
    """Complete cycle runs of the agent code `tree` on ref's tasks."""
    out = []
    for r in tsv_rows(common.BENCH / "results.tsv"):
        if (r["mode"], r["split"]) != ("cycle", ref["split"]) or not complete(r):
            continue
        if (r["task_set"], r["model_cfg"]) != (ref["task_set"], ref["model_cfg"]):
            continue
        if agent_tree(r["agent_sha"]) != tree:
            continue
        if (common.RESULTS_DIR / r["run_id"] / "rows.json").exists():
            out.append(with_outcomes(r))
    return out


# --------------------------------------------------------------------------- decisions

def screen(main_runs: list[dict], cand: dict) -> tuple[str, dict]:
    """A filter, not a verdict: beat main's mean by a task, with no new harm."""
    mean = statistics.mean(int(r["resolved"]) for r in main_runs)
    stats = {"main": round(mean, 2), "cand": cand["resolved"], "runs": len(main_runs)}
    if not complete(cand):
        return "incomplete", stats
    harm = (int(cand["false_merge"]) > max(int(r["false_merge"]) for r in main_runs)
            or int(cand["broke_other"]) > max(int(r["broke_other"]) for r in main_runs))
    stats["harm"] = "yes" if harm else ""
    if harm or int(cand["resolved"]) < mean + 1:
        return "discard", stats
    return "screen_pass", stats


def mcnemar_p(wins: int, losses: int) -> float:
    """One-sided exact McNemar: P(>= wins of wins+losses discordant pairs | no effect)."""
    n = wins + losses
    if n == 0:
        return 1.0
    return sum(math.comb(n, k) for k in range(wins, n + 1)) / 2 ** n


def paired_verdict(pairs: list[tuple[dict, dict]]) -> tuple[str, dict]:
    """Decide on (main, cand) replicate pairs of the same tasks.

    A win is a (task, replicate) the candidate resolved and main didn't; a loss
    the reverse. Keep needs p <= KEEP_P and no more false merges or broken
    tests than main. Runs out of replicates -> discard.
    """
    wins = sum(len(c["resolved_ids"] - m["resolved_ids"]) for m, c in pairs)
    losses = sum(len(m["resolved_ids"] - c["resolved_ids"]) for m, c in pairs)
    harm = any(sum(int(c[k]) for _, c in pairs) > sum(int(m[k]) for m, _ in pairs)
               for k in ("false_merge", "broke_other"))
    p = mcnemar_p(wins, losses)
    stats = {"main": sum(int(m["resolved"]) for m, _ in pairs),
             "cand": sum(int(c["resolved"]) for _, c in pairs), "runs": len(pairs),
             "wins": wins, "losses": losses, "p": round(p, 3), "harm": "yes" if harm else ""}
    if harm:
        return "discard", stats
    if p <= KEEP_P:
        return "keep", stats
    best_case = wins + (MAX_REPLICATES - len(pairs)) * int(pairs[0][0]["n"])
    if len(pairs) >= MAX_REPLICATES or mcnemar_p(best_case, losses) > KEEP_P:
        return "discard", stats
    return "confirming", stats


# --------------------------------------------------------------------------- editor

def code_context(src: Path) -> str:
    """The cycle code each program.md section feeds, so the editor knows its effect."""
    llm_src = (src / "ouroboros" / "llm.py").read_text()
    parts = [ast.get_source_segment(llm_src, node) for node in ast.parse(llm_src).body
             if isinstance(node, ast.FunctionDef) and node.name in (
                 "identify_improvements", "plan_code_change", "generate_code",
                 "review_code_changes", "get_tools_definition")]
    imp = (src / "ouroboros" / "improvement.py").read_text()
    a, b = imp.find("    # Handle Tool Calls"), imp.find("    task = ImprovementTask.from_llm_response")
    if 0 <= a < b:
        parts.append("# improvement._run_improvement_cycle, the ReAct loop after identify:\n"
                     + imp[a:b])
    return "\n\n".join(parts)


_SCRUB = (
    (re.compile(r"starts .*$"), "starts <reply>"),           # the model's own words
    (re.compile(r"(['\"]).*?\1"), "<quoted>"),
    (re.compile(r"[\w./-]+\.py\b(::[\w\[\]-]+)?"), "<file>"),
    (re.compile(r"\btest_\w+"), "<test>"),
    (re.compile(r"(Reviewer rejection|Out of scope):.*"), r"\1: <details>"),
)


def scrub(line: str) -> str:
    """An evidence line with nothing task-specific left: no files, tests or quotes."""
    for pattern, repl in _SCRUB:
        line = pattern.sub(repl, line)
    return line[:160]


EDITOR_SYSTEM = """You improve program.md, the file of system prompts that drives an autonomous \
code-fixing agent. Its cycle is: identify a task (with a ReAct tool loop), plan, generate \
SEARCH/REPLACE edits, peer review, run tests. Each section of program.md is the system prompt of \
one step; the code that sends each section is shown to you.

A fixed benchmark scores the agent: real bugs with failing tests it must find and fix. You get \
the agent's failure habits, most common first, and past experiments (kept and discarded).

Make ONE focused change that targets the top failure habit. Rules:
- Keep every section marker exactly: <!-- section: NAME --> ... <!-- end: NAME -->, same names.
- Change the text of one or two sections only. Keep each under twice its current length.
- Write general instructions. Never mention specific files, functions, tests or bugs: the \
benchmark has held-out tasks you cannot see, and an edit that only fits the visible ones is \
discarded.
- Don't retry a hypothesis that past experiments already discarded.

Reply in exactly this format and nothing else:
HYPOTHESIS: <one line: what you change and why it should fix the habit>
<<<PROGRAM
<the complete new program.md>
PROGRAM>>>"""


def editor_prompt(program: str, table: dict, past: list[dict], context: str) -> str:
    lines = ["## Failure habits (most common first)"]
    for sig, v in table.items():
        if sig == "resolved":
            lines.append(f"- resolved: {v['count']} tasks fixed")
            continue
        lines.append(f"- {sig}: {v['count']} tasks -- {v['meaning']}")
        for ev in sorted({scrub(t["evidence"]) for t in v["tasks"] if t["evidence"]})[:3]:
            lines.append(f"    log: {ev}")
    lines.append("\n## Past experiments (newest last)")
    if not past:
        lines.append("none yet")
    for e in past:
        lines.append(f"- [{e.get('decision')}] habit={e.get('habit')}: {e.get('hypothesis')}")
    return (f"## Current program.md\n{program}\n\n" + "\n".join(lines)
            + f"\n\n## Cycle code that sends each section (read-only)\n```python\n{context}\n```")


def parse_reply(text: str) -> tuple[str, str]:
    m = re.search(r"HYPOTHESIS:\s*(.+)", text or "")
    p = re.search(r"<<<PROGRAM\n(.*?)\n?PROGRAM>>>", text or "", re.S)
    if not (m and p):
        raise ValueError("reply is not HYPOTHESIS + <<<PROGRAM ... PROGRAM>>>")
    return m.group(1).strip()[:300], p.group(1).replace("\r\n", "\n").rstrip("\n") + "\n"


def leak_terms() -> set[str]:
    """Names a task-specific edit would mention: the F2P test functions."""
    terms = set()
    for d in common.TASKS_DIR.iterdir():
        if (d / "task.json").exists():
            for node in json.loads((d / "task.json").read_text())["f2p"]:
                name = node.split("::")[-1].split("[")[0]
                if len(name) >= 8:
                    terms.add(name)
    return terms


def validate(old: str, new: str, leaks: set[str]) -> list[str]:
    errors = []
    before, after = dict(SECTION_RE.findall(old)), dict(SECTION_RE.findall(new))
    if set(before) != set(after):
        errors.append(f"sections changed: {sorted(before)} -> {sorted(after)}")
    if new == old:
        errors.append("no change")
    for name, text in after.items():
        if not text.strip():
            errors.append(f"section {name} is empty")
        if name in before and len(text) > 2 * max(len(before[name]), 200):
            errors.append(f"section {name} grew more than 2x")
    named = sorted(t for t in leaks if t in new and t not in old)
    if named:
        errors.append(f"names benchmark tests: {named[:3]}")
    return errors


class Budget:
    """This invocation's requests, inside what the daily cap has left."""

    def __init__(self, total: int):
        self.total, self.used = min(total, common.daily_left()), 0

    @property
    def left(self) -> int:
        return self.total - self.used

    def charge(self, source: str, n: int) -> None:
        self.used += n
        common.ledger_add(source, n)


class Editor:
    def __init__(self, model: str, budget: Budget):
        from openai import OpenAI
        self.client = OpenAI(base_url=OPENROUTER, api_key=os.environ["BENCH_LLM_API_KEY"],
                             max_retries=0)
        self.model, self.budget = model, budget

    def ask(self, system: str, user: str) -> str:
        from ouroboros.retry import is_daily_quota_exhausted
        if self.budget.left < 1:
            raise BudgetExhausted("no requests left for the editor")
        self.budget.charge("outer-editor", 1)
        try:
            resp = self.client.chat.completions.create(
                model=self.model, max_tokens=16000,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        except Exception as exc:
            if is_daily_quota_exhausted(exc):
                raise BudgetExhausted("daily quota 429 (editor)") from exc
            raise
        return resp.choices[0].message.content or ""


# --------------------------------------------------------------------------- runs

def bench(agent_src: Path, split: str, budget: Budget, note: str) -> dict:
    """One run.py cycle run (it charges the ledger itself); its results.tsv row."""
    cmd = [sys.executable, str(common.BENCH / "run.py"), "--mode", "cycle", "--split", split,
           "--agent-src", str(agent_src), "--max-requests", str(budget.left), "--note", note]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"^(\S+-cycle-\w+):", proc.stdout, re.M)
    if proc.returncode != 0 or not m:
        raise RuntimeError(f"run.py failed ({proc.returncode}): {proc.stderr[-500:]}")
    row = next(r for r in tsv_rows(common.BENCH / "results.tsv") if r["run_id"] == m.group(1))
    budget.used += int(row["llm_requests"])
    print(f"  {split} {note}: resolved {row['resolved']}/{row['scored']} (n={row['n']}), "
          f"{row['llm_requests']} requests", flush=True)
    return with_outcomes(row)


def need(split: str, budget: Budget) -> None:
    if budget.left < NEED[split]:
        raise BudgetExhausted(f"{split} needs ~{NEED[split]} requests, {budget.left} left "
                              f"(daily cap {common.DAILY_CAP}, {common.ledger_used()} spent today)")


class Worktree:
    def __init__(self, ref: str):
        self.path = Path(tempfile.mkdtemp(prefix="bench-outer-")) / "wt"
        git("worktree", "add", "-q", "--detach", str(self.path), ref)

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *exc) -> None:
        subprocess.run(["git", "-C", str(common.REPO), "worktree", "remove", "--force",
                        str(self.path)], capture_output=True)
        shutil.rmtree(self.path.parent, ignore_errors=True)


# --------------------------------------------------------------------------- steps

def new_experiment(args, budget: Budget, base: str) -> dict:
    exp = {"exp_id": f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}", "step": "screen",
           "base_sha": base, "split": "smoke", "editor_model": args.editor_model}
    ref, tree = run_ref("smoke"), agent_tree(base)
    main_runs = scored_runs(tree, ref)
    if len(main_runs) < MIN_MAIN_RUNS:
        need("smoke", budget)
        bench(common.REPO / "src", "smoke", budget, "outer: main reference")
        return {**exp, "step": "calibrate", "decision": "calibrating",
                "note": f"main smoke runs: {len(scored_runs(tree, ref))}/{MIN_MAIN_RUNS}"}
    table = habits.habits(common.RESULTS_DIR / max(r["run_id"] for r in main_runs))
    exp["habit"] = habits.top_habit(table)
    if exp["habit"] is None:
        return {**exp, "decision": "nothing_to_fix"}
    need("smoke", budget)

    program = (common.REPO / PROGRAM_REL).read_text()
    past = [e for e in tsv_rows(EXPERIMENTS) if e["step"] == "screen"][-20:]
    user = editor_prompt(program, table, past, code_context(common.REPO / "src"))
    editor, leaks = Editor(args.editor_model, budget), leak_terms()
    new, errors = "", ["no reply"]
    for attempt in range(2):
        reply = editor.ask(EDITOR_SYSTEM, user if attempt == 0 else
                           f"{user}\n\n## Your previous reply was rejected\n- " + "\n- ".join(errors)
                           + "\nReply again in the exact format.")
        try:
            exp["hypothesis"], new = parse_reply(reply)
            errors = validate(program, new, leaks)
        except ValueError as exc:
            errors = [str(exc)]
        if not errors:
            break
    if errors:
        return {**exp, "decision": "invalid_edit", "note": "; ".join(errors)[:300]}

    out = common.RESULTS_DIR / f"outer-{exp['exp_id']}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "editor_prompt.md").write_text(user)
    branch = f"bench/program-{exp['exp_id']}"
    with Worktree(base) as wt:
        (wt / PROGRAM_REL).write_text(new)
        git("switch", "-q", "-c", branch, cwd=wt)
        git("-c", "user.name=bench-outer", "-c", "user.email=bench@localhost", "commit", "-q",
            "--no-verify", "-am", f"program.md: {exp['hypothesis'][:60]}\n\nhabit: {exp['habit']}\n"
            f"outer-loop experiment {exp['exp_id']}", cwd=wt)
        exp["cand_sha"], exp["branch"] = git("rev-parse", "--short", "HEAD", cwd=wt).strip(), branch
        (out / "program.diff").write_text(git("diff", base, "HEAD", "--", PROGRAM_REL, cwd=wt))
        cand = bench(wt / "src", "smoke", budget, f"outer {exp['exp_id']}")
    exp["decision"], stats = screen(main_runs, cand)
    exp.update(stats)
    if exp["decision"] != "screen_pass":
        subprocess.run(["git", "-C", str(common.REPO), "branch", "-D", branch], capture_output=True)
        exp["note"] = f"branch deleted; diff in bench/results/{out.name}/"
    return exp


def confirm(args, budget: Budget, base: str) -> dict:
    """One paired replicate on confirm: fresh main and candidate runs, same tasks."""
    rows = history(args.confirm)
    screened = next((e for e in rows if e["decision"] == "screen_pass"), None)
    if screened is None:
        raise SystemExit(f"{args.confirm}: no screen_pass in {EXPERIMENTS.name}")
    if rows[-1]["decision"] not in ("screen_pass", "confirming", "out_of_budget"):
        raise SystemExit(f"{args.confirm} is already decided: {rows[-1]['decision']}")
    if agent_tree(screened["base_sha"]) != agent_tree(base):
        raise SystemExit(f"{args.confirm} was screened against {screened['base_sha']}; "
                         f"the agent code at HEAD ({base}) differs")
    state = common.RESULTS_DIR / f"outer-{args.confirm}" / "confirm.json"
    done = json.loads(state.read_text()) if state.exists() else []
    need("confirm", budget)
    with Worktree(screened["branch"]) as wt:
        order = [("main", common.REPO / "src"), ("cand", wt / "src")]
        if len(done) % 2:
            order.reverse()  # alternate, so drift during a day hits both sides
        pair = {who: bench(src, "confirm", budget, f"outer confirm {args.confirm} {who}")
                for who, src in order}
    if not (complete(pair["main"]) and complete(pair["cand"])):
        decision, stats = "confirming", {"note": "replicate incomplete (budget/quota), not counted"}
    else:
        done.append({"main": pair["main"]["run_id"], "cand": pair["cand"]["run_id"]})
        state.write_text(json.dumps(done, indent=2))
        pairs = [(with_outcomes(next(r for r in tsv_rows(common.BENCH / "results.tsv")
                                     if r["run_id"] == d["main"])),
                  with_outcomes(next(r for r in tsv_rows(common.BENCH / "results.tsv")
                                     if r["run_id"] == d["cand"]))) for d in done]
        decision, stats = paired_verdict(pairs)
    return {**{k: screened.get(k, "") for k in ("exp_id", "base_sha", "cand_sha", "branch",
                                                 "editor_model", "habit", "hypothesis")},
            **stats, "step": "confirm", "split": "confirm", "decision": decision}


def heldout(args, budget: Budget, base: str) -> dict:
    """Does a kept edit's gain carry over to tasks the loop never saw?"""
    rows = history(args.heldout)
    kept = next((e for e in rows if e["decision"] == "keep"), None)
    if kept is None:
        raise SystemExit(f"{args.heldout}: not kept")
    need("heldout", budget)
    with Worktree(kept["branch"]) as wt:
        m = bench(common.REPO / "src", "heldout", budget, f"outer heldout {args.heldout} main")
        c = bench(wt / "src", "heldout", budget, f"outer heldout {args.heldout} cand")
    decision, stats = paired_verdict([(m, c)])
    ok = complete(m) and complete(c) and not stats["harm"] and stats["wins"] >= stats["losses"]
    exp = {**{k: kept.get(k, "") for k in ("exp_id", "base_sha", "cand_sha", "branch",
                                           "editor_model", "habit", "hypothesis")},
           **stats, "step": "heldout", "split": "heldout",
           "decision": "heldout_pass" if ok else "heldout_fail"}
    if ok and args.open_pr:
        exp["note"] = open_pr(exp, kept)
    elif not ok:
        exp["note"] = "dev gain did not carry over to heldout: overfit, don't merge"
    return exp


def open_pr(exp: dict, kept: dict) -> str:
    git("push", "-q", "-u", "origin", exp["branch"])
    body = (f"Outer-loop experiment `{exp['exp_id']}` (bench/outer.py).\n\n"
            f"- habit targeted: `{exp['habit']}`\n- hypothesis: {exp['hypothesis']}\n"
            f"- confirm (paired, {kept['runs']} replicates): {kept['wins']} wins / "
            f"{kept['losses']} losses, McNemar p={kept['p']}\n"
            f"- heldout: {exp['wins']} wins / {exp['losses']} losses\n"
            f"- editor: `{exp['editor_model']}` via OpenRouter\n\n"
            "Kept by the bench; merged by a human.\n\n"
            "🤖 Generated with [Claude Code](https://claude.com/claude-code)")
    proc = subprocess.run(["gh", "pr", "create", "--base", "main", "--head", exp["branch"],
                           "--title", f"program.md: {exp['hypothesis'][:60]}", "--body", body],
                          cwd=common.REPO, capture_output=True, text=True)
    return proc.stdout.strip() or proc.stderr.strip()[:200]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-requests", type=int, default=common.DAILY_CAP,
                    help="for this invocation; the daily cap (BENCH_DAILY_CAP) applies on top")
    ap.add_argument("--editor-model", default=EDITOR_MODEL)
    ap.add_argument("--confirm", metavar="EXP_ID", help="one paired replicate for a screen pass")
    ap.add_argument("--heldout", metavar="EXP_ID", help="heldout check for a kept edit")
    ap.add_argument("--open-pr", action="store_true", help="with --heldout: open a PR if it passes")
    args = ap.parse_args()
    if not os.environ.get("BENCH_LLM_API_KEY"):
        print("BENCH_LLM_API_KEY is not set (the OpenRouter key production uses)", file=sys.stderr)
        return 2
    if git("status", "--porcelain", "--", "src/ouroboros").strip():
        print("src/ouroboros has uncommitted changes; main must be a commit", file=sys.stderr)
        return 2
    sys.path.insert(0, str(common.REPO / "src"))
    base = git("rev-parse", "--short", "HEAD").strip()
    budget = Budget(args.max_requests)
    step = confirm if args.confirm else heldout if args.heldout else new_experiment
    try:
        exp = step(args, budget, base)
    except BudgetExhausted as exc:
        exp = {"exp_id": args.confirm or args.heldout or "-", "step": step.__name__,
               "base_sha": base, "decision": "out_of_budget", "note": str(exc)}
    exp.update(date=time.strftime("%Y-%m-%d"), requests=budget.used)
    append_experiment(exp)
    print(json.dumps({k: exp.get(k, "") for k in EXP_FIELDS}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
