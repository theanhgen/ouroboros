# bench — a fixed scoreboard for the improvement pipeline

Status: Phase 1 built and baselined 2026-10-05. Phase 2 approved 2026-10-05 (OpenRouter only); steps 1-3 and 5 being built, step 4 deferred.

## Why

The cycle merges any change that doesn't regress the tests and that the reviewer model
approves. Nothing measures whether the agent got *better*. Evidence from the last month on
`main`:

- duplicate merges: #383 and #386 (same backlog bug), #384 and #385 (same tests),
  #393, #394 and #398 (same `extract_failure_location` helper);
- `fix_bug` is 0/13 by its own metrics;
- in the last 24h on rubrum, about 65 cycles produced 3 PRs. 59 identified nothing
  ("named no task" 12, "unparseable final answer" 16, "No improvements identified" 31).

karpathy/autoresearch works because every change is judged by one fixed number (val_bpb)
that the agent cannot edit, and is kept only if that number improves. This plan builds
the number for ouroboros.

## Phase 1 — benchmark + baseline (this change)

### Tasks: seeded bugs mined from the repo's own history

Candidates are non-merge commits whose subject is a fix (`fix…`, `ouroboros: fix_bug`) and
which touch both `src/ouroboros/*.py` and `tests/*.py`. 75 exist today.

For a candidate fix commit `C` with parent `P`:

- **snapshot** = `P`'s tree + `C`'s changes to test files only (SWE-bench style: the new
  failing tests are visible, the fix is not);
- **F2P** (fail-to-pass) = test ids in `C`'s changed test files that fail on the snapshot
  and pass on `C`;
- **P2P** (pass-to-pass) = test ids in those files that pass on both.

A task is kept only if:

1. F2P is non-empty;
2. `C`'s source diff (excluding tests) fits the production caps: ≤3 files and ≤200 changed
   lines;
3. `C`'s source diff touches no `forbidden_modification_paths` file, since the agent cannot
   edit those and the task would be unsolvable by design;
4. it is deterministic: F2P fails on the snapshot and passes on `C` in 3 of 3 runs each;
5. the snapshot's targeted tests finish in under 60s.

Stored per task in `bench/tasks/<id>/`: `task.json` (commit, parent, date, subject, F2P,
P2P), `test.patch`, and `gold.patch` (used only to verify the task, never shown to the
agent).

Split: tasks are sorted by commit date and every third one goes into a held-out set that
is never used for decisions. It only gets checked occasionally, to detect the dev set
being overfit.

### What runs: the real cycle, sandboxed

The thing under test is the pipeline at the harness checkout's HEAD: prompts, llm, backends
and the cycle itself. For each task the harness:

1. materializes the snapshot with `git archive P` plus `test.patch` into a temp dir and
   strips all agent state (`common.STATE_PATHS`: the databases, `learnings.md`,
   `backlog.json`, `state.json`, `metrics.json`, the legacy `improvement_history.json`
   that `load_history` would re-import, `.ouroboros/`, `docs/wiki`, `ISSUES.md`,
   `MERGED/`). It then `git init`s it with one commit and **no remote**. No history means
   the fix can't leak; no remote means a push is impossible even if a stub is missed. The
   worker asserts `load_history(snapshot)` is empty before any LLM call;
2. runs the worker under `sandbox-exec`. Writes are allowed only in the task's own temp
   dir and the run's shared counter dir. There are no reads of `$HOME` (except the uv
   Python install), `bench/tasks` (the gold patches) or `bench/results`. Network stays
   open for the LLM gateway; the key is dropped from the environment as soon as the
   client is built, so the pytest children never see it. `HOME` is a temp dir, `cwd` is
   the snapshot, a fake `gh` that always fails is first on `PATH`, and
   `PYTHONPATH=<snapshot>/src` is set only for children. A probe asserts that the worker
   imports the harness's `ouroboros` and the children import the snapshot's;
3. calls the real `improvement.run_improvement_cycle` with production's
   `config/agent.json` (models, fallbacks, reviewer), `enable_auto_merge=False`, and stubs
   for `git_ops.has_open_improvement_prs` (→ False), `create_branch`, `commit_changes`,
   `push_branch`, `create_pr` and `auto_merge_pr` (record only), plus `checkout_branch`;
4. after the cycle, takes only the agent's diff under `src/ouroboros/` and applies it to
   a **fresh, pristine** snapshot. F2P, P2P and the full-suite baseline are all measured
   there. Nothing the agent did to `tests/`, a `conftest.py`, pytest config or any other
   file can reach the score.

Fresh-agent conditions: empty history, memory, backlog and feed. That makes every task
independent and repeatable. It also means the benchmark measures the pipeline, not
accumulated memory (stated as a limitation below).

### Metrics (one row per run, appended to `bench/results.tsv`)

- **resolved** — all F2P pass and all P2P pass. **Primary metric:** `resolved / N`.
- **false_merge** — the cycle ended `success` (it would have opened a PR) but the task is
  not resolved. This measures the validation gate directly: with failing tests already
  present, "no regression" passes a change that fixes nothing.
- **broke_p2p** — the cycle ended `success` but P2P regressed.
- **identified_nothing / plan_fail / gen_fail / review_reject / reverted** — where the
  pipeline lost the task.
- cost: LLM requests, prompt/completion tokens, wall time per task.

Each row also carries the run id, the agent's git sha, the model config hash, the task-set
hash, N and k. Per-task JSON with the cycle log goes in `bench/results/<run-id>/` (gitignored
except the summary).

### Budget and where it runs

- It runs on Elaeis, not on rubrum: never write on the Pi, and keep its CPU free for the
  live loop.
- LLM: production's models through OpenRouter, **on the same key as production**.
  Production made ~491 requests in the last 24h, and the free cap is 1000/day per
  account. So the harness counts requests on the client and stops at `--max-requests`
  (default 300). The first daily-quota 429 stops the whole run, because the remaining
  quota belongs to the live agent. Tasks hit by the budget, the quota or an auth error
  are marked `invalid` and left out of the score, never counted as failures. Production
  itself switches to its overflow gateway on a daily-quota 429; the bench has no
  overflow, so it can never score a different model by accident. k runs are spread
  across days.
- Workers: 2 in parallel for `cycle` (free models are rate-limited per minute too), 6 for
  `gold`/`null`.

### Code layout

`bench/` at the repo root, outside `allowed_modification_paths` (`src/ouroboros/`,
`tests/`, `docs/wiki/`), so the agent cannot edit the benchmark, the harness or the
results.

- `bench/build_tasks.py` — mine, verify and write tasks (no LLM).
- `bench/run.py` — run the tasks k times and append the results.
- `bench/README.md` — how to run and read it.
- `bench/tests/` — harness unit tests, run explicitly; not in the main `testpaths`.

Merging `bench/` to `main` does not restart the Pi (`pull_latest` only reacts to `src/`
changes) and adds nothing to the Pi's test run. The PR branch must not start with
`ouroboros/improve-`, or it would block the live cycle.

### Done when

- the task set is built and verified (target ≥20 tasks), and every gold patch resolves
  its own task in the harness (the oracle run = 100%);
- the null patch resolves 0% (a no-change run);
- the baseline: one full run of the current `main` pipeline on the dev set, numbers in
  `results.tsv`;
- a PR is open, not merged.

### Baseline (2026-10-05, `main` 579e890f, production models)

| run | tasks | resolved | false_merge | broke_other | requests |
|---|---|---|---|---|---|
| gold (oracle control) | 43 | 43 (100%) | 0 | 0 | 0 |
| null (no-change control) | 43 | 0 (0%) | 0 | 0 | 0 |
| **cycle, dev** | 29 | **2 (6.9%)** | 0 | 0 | 195 |

Where the 27 misses went:

- 23 `idle`, with no task identified even though failing tests were put in front of it.
  18 of them: the ReAct loop's final answer came back empty ("unparseable final answer").
- 4 `failed`: 2 generations that returned nothing, 1 truncated at max_tokens, and 1 task
  that targeted a forbidden file.

The pipeline almost never reaches the code-writing step. When it does (4 of 29), it
fixes half. The first lever is the identify/ReAct step, not generation.

## Plan review (2026-10-05)

A dual review ran on this plan: codex, agy (Gemini) and agy-gpt. agy-claude failed on a
stale model pin, so it was 3 of 4 reviewers. Confirmed and fixed above:

- client-side counting can't see production's spend → stop on the first daily-quota 429
  and run 2 workers;
- HOME + cwd doesn't contain generated code → `sandbox-exec` jail, key dropped from the
  children;
- the plan's strip list missed `improvement_history.json` (the code already stripped it)
  → added an empty-history assertion;
- restoring only the task's test files left `conftest.py` and helpers editable → scoring
  moved to a fresh snapshot with only the `src/ouroboros/` diff.

Refuted: the targeted-vs-full-suite timeout (the slowest snapshot's full suite takes
14.9s, against `run_tests`' 120s), CI cost or DoS (CI runs only `pytest tests/`), and a
"metrics leak" (the repo is public and already publishes `docs/wiki/metrics.md`).

## Phase 2 — the outer loop: rewrite "how to work", keep only if the score rises

Source: the AI Labs video "He Finally 10x Claude Code With This Method" (NotebookLM
notebook "Mastering AI Agent Loops with the Karpathy Method", read 2026-10-05). Mapped
onto ouroboros:

| Karpathy loop / video | ouroboros today | after Phase 2 |
|---|---|---|
| `prepare.py` / locked checks the agent can't edit | none: the gate is "no regression" | `bench/` (outside `allowed_modification_paths`) |
| `train.py`, the one file the agent changes | any file under `src/`, `tests/` | unchanged for the normal cycle |
| `program.md` "how to work", written by a human | prompts inline in `llm.py` functions | **one file the outer loop owns**: `src/ouroboros/program.md` |
| results file: every round, kept or undone, what failed | `improvement_history` (no score) | `bench/results.tsv` + per-task rows |
| inner loop: fresh agent per feature | fresh cycle per task | unchanged |
| **outer loop**: reads results, finds recurring habits, rewrites `program.md`, can't touch checks | **missing** | `bench/outer.py` |

The video's one finding worth taking: an inner loop repeats the same mistakes, because
every round starts from the same instructions and nothing it learns carries forward. The
baseline shows exactly that. Most dev tasks end `idle`: the ReAct loop's final answer is
empty, or the free model returns an empty completion. That is production's
59-idle-of-65 pattern, and a "habit" an outer loop can target.

### Constraint: everything runs on OpenRouter (owner, 2026-10-05)

Every model call the bench and the outer loop make goes through the OpenRouter key that
production already uses: free models only, no second account, no Codex or other runtime in
the loop. That key has ~1000 free requests/day, and production spends ~500 of them. So:

- **one shared request budget.** `outer.py` takes `--max-requests` for the whole
  experiment (editor + both bench runs) and stops on the first daily-quota 429, the same
  way `run.py` does;
- **the editor is a free model.** `nvidia/nemotron-3-ultra-550b-a55b:free` by default,
  the largest free model and the one production already trusts as its reviewer.
  `--editor-model` overrides it. It gets one request per experiment, two if the first
  reply is unusable;
- **screening on a `smoke` split.** 10 fixed dev tasks: the 6 that got past identify in
  the baseline (both resolved ones included, so a lost fix shows) plus 4 idle ones spread
  across the date range. That's ~70 requests per run. A candidate that wins on `smoke`
  gets one full `dev` run before it's kept;
- **`main` is scored once, not per experiment.** The reference for a task set is the mean
  of every `main` run at the same `agent_sha`, `model_cfg` and `task_set` already in
  `results.tsv`. An experiment pays only for the candidate's run, unless `main` has fewer
  than 2 runs on that set.

That's ~3–4 screening experiments a day, against autoresearch's 100 a night. Expect weeks,
not a night.

### Steps

1. **Move the cycle's prompts into `src/ouroboros/program.md`** (operator commit,
   behaviour-neutral). The sections are `identify`, `react_final`, `plan`, `edit` and
   `review`: the system prompts now inline in `llm.identify_improvements`,
   `plan_code_change`, `_EDIT_SYSTEM_PROMPT` and `review_code_changes`, plus
   `improvement._REACT_FINAL_PROMPT`. A loader in `prompts.py` reads named sections from
   the file next to the module (never the cwd: under the bench, the cwd is a task
   snapshot holding an older `program.md`). A missing section is an error, not a silent
   fallback, so a broken edit fails loudly instead of quietly scoring the old prompt.
   - **`program.md` is added to `forbidden_modification_paths`**, as a bare filename. Today
     `src/ouroboros/` is in `allowed_modification_paths`, so the live cycle could rewrite
     the very file the outer loop is scoring. Changing it becomes an operator or
     outer-loop PR.
   - **Proof of no behaviour change, with no LLM spend:** a one-off check that every
     section is byte-identical to the string it replaces (run against the parent commit,
     output in the PR), the full suite, and gold/null still at 43/0. A bench run can't
     prove it, because free models differ run to run.
   - `pyproject.toml` ships `program.md` as package data.
2. **Failure taxonomy** (`bench/habits.py`, deterministic, no LLM). Each row plus its
   worker log gets exactly one signature, the first that matches, in pipeline order:
   `out_of_scope`, `identify_error`, `react_empty_final`, `named_no_task`,
   `no_task_other`, `plan_failed`, `generate_empty`, `generate_truncated`,
   `edit_mismatch`, `reviewer_rejected`, `tests_regressed`, `false_merge`,
   `partial_fix` (some F2P pass), `wrong_fix`, `resolved`. Output: counts per signature,
   evidence task ids, and a line or two of log for each. The baseline run classifies as
   18 `react_empty_final`, 5 other no-task, 2 `generate_empty`, 1 `generate_truncated`,
   1 `out_of_scope`, 2 `resolved`.
3. **Outer loop** (`bench/outer.py`, on Elaeis), one experiment per invocation:
   - read the habit table for the newest `main` run on `smoke` (or `dev`) and the last
     20 rows of `bench/experiments.tsv`, discarded ones included, so it doesn't retry them;
   - send the editor model `program.md`, the habit table with evidence, the cycle code
     that uses each section (read-only context, so it knows what the prompts feed), and
     the past experiments. It must reply with a full new `program.md` and a one-line
     hypothesis;
   - **validate before spending:** same section names, only `program.md` changes, each
     section within 2× its old size, `{placeholders}` (if any) kept. A violation is
     logged as `invalid_edit`, at the cost of one request;
   - commit the candidate on a local branch `bench/program-<id>` in a temp worktree (so
     `agent_sha` differs from `main`) and run `run.py --agent-src <worktree>/src` on
     `smoke`;
   - **keep** if candidate resolved ≥ main mean + 1, `false_merge` and `broke_other`
     don't rise, and no task `main` resolved in every one of its runs is lost. Then run
     `dev` once, and keep for good only if `dev` resolved ≥ the `main` dev mean. Otherwise
     discard;
   - log every experiment (id, date, habit, hypothesis, editor model, smoke/dev numbers,
     keep/discard/invalid, requests, branch) to `bench/experiments.tsv`. Kept branches
     stay local. Opening a PR is `--open-pr`, off by default; the owner merges.
   - Hard limits: it writes only `program.md`, in a temp worktree. The editor model gets
     no tools and no file access, and never sees `bench/tasks` (gold patches) or test
     contents, only signatures and log lines.
4. **Merge gate for pipeline changes**: deferred. It's a production gate (operator commit
   to `evaluation.py`/`git_ops.py`) and needs a bench run reachable from rubrum. Revisit
   once the outer loop has kept something.
5. **Held-out check**: `outer.py --heldout` runs `heldout` for `main` and for the newest
   kept branch. Run it every ~10 kept experiments. If `dev` rises and `heldout` doesn't,
   the dev set is being overfit: stop and mine new tasks.

### Already visible in the baseline, outside the outer loop's reach

18 of 29 dev tasks hit 7 requests: identify, 5 ReAct rounds that keep calling tools, then
the forced final answer comes back as `''`. In `improvement._run_improvement_cycle` the
ReAct rounds and the forced final call send `response_format=json_object` with no `tools`
argument, while the history holds tool calls. That may be what makes free models answer
empty. It's a hypothesis, not a verified cause. The outer loop can't test it, because it
may only edit `program.md`. It's an operator experiment: bench `main` against a branch
that changes the round calls, then decide on the numbers.

### What is not taken from the video

- Human-approved, newly written checks per feature. Our checks are historical tests,
  approved by construction. For the live cycle's own work, the reviewer + test gate
  stays.
- The "project context" skill. ouroboros already has `codebase.get_codebase_summary` +
  memory; whether that context helps is something the outer loop can measure, not
  something to add blind.
- Their sandbox vendor. `sandbox-exec` on Elaeis covers this.

## Known limitations

- It measures "fix the code given a failing test". The live `fix_bug` lane mostly hunts
  bugs in green code, which this does not measure.
- Fresh-agent conditions exclude memory and backlog effects.
- Free models make the runs noisy. With N≈25, one task is 4 points. Only paired, multi-run
  differences mean anything.
- Historical snapshots run with today's dependency versions. Tasks that don't run are
  dropped, which biases the set toward recent history.
