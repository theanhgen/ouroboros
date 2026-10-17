#!/usr/bin/env python3
"""Mine seeded-bug tasks from this repository's own fix commits.

For a fix commit C with parent P, the task is P's tree plus C's test changes
(the new failing tests are visible, the fix is not). F2P are the tests in C's
changed test files that pass with the fix and not without it; P2P pass both
ways. A task is kept only if it is solvable inside the agent's own limits and
deterministic. See bench/PLAN.md.

No LLM calls. Writes bench/tasks/<id>/{task.json,test.patch,gold.patch}.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

SUBJECT_RE = re.compile(r"^(fix\b|fix\(|ouroboros: fix_bug|ouroboros: fix_test)", re.I)
MAX_FILES = 3
MAX_LINES = 200
REPEATS = 3
TARGETED_TIMEOUT = 60


def candidates(repo: Path) -> list[dict]:
    out = common.git(repo, "log", "--no-merges", "--format=@@%H\x1f%P\x1f%ad\x1f%s",
                     "--date=short", "--name-only")
    found, cur = [], None
    for line in out.splitlines() + ["@@"]:
        if line.startswith("@@"):
            if cur and cur["src"] and cur["tests"] and SUBJECT_RE.search(cur["subject"]):
                found.append(cur)
            cur = None
            if line != "@@":
                sha, parents, date, subject = line[2:].split("\x1f", 3)
                if len(parents.split()) == 1:
                    cur = {"commit": sha, "parent": parents, "date": date,
                           "subject": subject, "src": [], "tests": []}
            continue
        if cur is None or not line.strip():
            continue
        if line.startswith("src/ouroboros/") and line.endswith(".py"):
            cur["src"].append(line)
        elif line.startswith("tests/") and line.endswith(".py"):
            cur["tests"].append(line)
    return found


def source_diff_size(repo: Path, cand: dict) -> tuple[int, int]:
    numstat = common.git(repo, "diff", "--numstat", cand["parent"], cand["commit"], "--", "src/")
    files, lines = 0, 0
    for row in numstat.splitlines():
        added, deleted, _ = row.split("\t", 2)
        files += 1
        lines += int(added) + int(deleted) if added != "-" else 10**6
    return files, lines


def build_one(repo: Path, cand: dict, forbidden: tuple[str, ...]) -> dict:
    cid = f"{cand['date']}-{cand['commit'][:8]}"
    verdict = {"id": cid, "commit": cand["commit"], "subject": cand["subject"]}

    hit = [f for f in cand["src"] if Path(f).name in forbidden]
    if hit:
        return {**verdict, "drop": f"fix touches forbidden file(s): {', '.join(hit)}"}
    files, lines = source_diff_size(repo, cand)
    if files > MAX_FILES or lines > MAX_LINES:
        return {**verdict, "drop": f"fix exceeds caps: {files} files, {lines} lines"}

    test_patch = common.git(repo, "diff", "--binary", cand["parent"], cand["commit"], "--", "tests/")
    gold_patch = common.git(repo, "diff", "--binary", cand["parent"], cand["commit"], "--", "src/")
    # Test files that exist after the fix; deleted ones cannot be run.
    test_files = [f for f in cand["tests"]
                  if common.git_ok(repo, "cat-file", "-e", f"{cand['commit']}:{f}")]
    if not test_files:
        return {**verdict, "drop": "no surviving test files"}

    work = Path(tempfile.mkdtemp(prefix=f"bench-build-{cid}-"))
    try:
        snap = work / "snap"
        common.materialize(repo, cand["parent"], snap, test_patch)

        def outcomes() -> list[dict]:
            runs = []
            for _ in range(REPEATS):
                res = common.run_pytest(snap, test_files, timeout=TARGETED_TIMEOUT)
                if res["timed_out"]:
                    raise TimeoutError
                runs.append(res["outcomes"])
            return runs

        try:
            before = outcomes()
            common.apply_patch(snap, gold_patch)
            after = outcomes()
        except TimeoutError:
            return {**verdict, "drop": f"targeted tests exceed {TARGETED_TIMEOUT}s"}
        except common.PatchError as e:
            return {**verdict, "drop": f"patch does not apply: {e}"}

        def passed(run: dict) -> set:
            return {t for t, o in run.items() if o == "passed"}

        before_pass = [passed(r) for r in before]
        after_pass = [passed(r) for r in after]
        if any(p != before_pass[0] for p in before_pass) or any(p != after_pass[0] for p in after_pass):
            return {**verdict, "drop": "flaky: outcomes differ across repeats"}

        f2p = sorted(after_pass[0] - before_pass[0])
        p2p = sorted(after_pass[0] & before_pass[0])
        lost = sorted(before_pass[0] - after_pass[0])
        if not f2p:
            return {**verdict, "drop": "no fail-to-pass test"}
        if lost:
            return {**verdict, "drop": f"fix breaks {len(lost)} previously passing test(s)"}

        # The cycle runs the whole suite several times; it must at least run.
        full = common.run_pytest(snap, [], timeout=300)
        if full["timed_out"] or not full["outcomes"]:
            return {**verdict, "drop": "full suite does not run on the fixed snapshot"}

        task = {
            "id": cid,
            "commit": cand["commit"],
            "parent": cand["parent"],
            "date": cand["date"],
            "subject": cand["subject"],
            "test_files": test_files,
            "gold_files": [f for f in cand["src"]],
            "gold_lines": lines,
            "f2p": f2p,
            "p2p": p2p,
            "full_suite_seconds": round(full["seconds"], 1),
        }
        return {**verdict, "task": task, "test_patch": test_patch, "gold_patch": gold_patch}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=common.REPO)
    ap.add_argument("--out", type=Path, default=common.TASKS_DIR)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="only the first N candidates (debug)")
    args = ap.parse_args()

    forbidden = common.forbidden_paths()
    cands = candidates(args.repo)
    if args.limit:
        cands = cands[: args.limit]
    print(f"{len(cands)} candidate fix commits", flush=True)

    t0 = time.time()
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda c: build_one(args.repo, c, forbidden), cands))

    kept = sorted((r for r in results if "task" in r), key=lambda r: (r["task"]["date"], r["id"]))
    for i, r in enumerate(kept):
        # Every third task, by date, is held out and never used for decisions.
        r["task"]["split"] = "heldout" if i % 3 == 2 else "dev"

    if args.out.exists():
        shutil.rmtree(args.out)
    for r in kept:
        d = args.out / r["id"]
        d.mkdir(parents=True)
        (d / "task.json").write_text(json.dumps(r["task"], indent=2) + "\n")
        (d / "test.patch").write_text(r["test_patch"])
        (d / "gold.patch").write_text(r["gold_patch"])

    report = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "head": common.git(args.repo, "rev-parse", "HEAD").strip(),
        "candidates": len(cands),
        "kept": len(kept),
        "dev": sum(r["task"]["split"] == "dev" for r in kept),
        "heldout": sum(r["task"]["split"] == "heldout" for r in kept),
        "dropped": [{"id": r["id"], "subject": r["subject"][:90], "why": r["drop"]}
                    for r in results if "drop" in r],
    }
    (args.out / "BUILD.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"kept {report['kept']} ({report['dev']} dev, {report['heldout']} held out) "
          f"of {report['candidates']} in {time.time() - t0:.0f}s", flush=True)
    reasons: dict[str, int] = {}
    for d in report["dropped"]:
        key = d["why"].split(":")[0]
        reasons[key] = reasons.get(key, 0) + 1
    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  dropped {v:3d}  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
