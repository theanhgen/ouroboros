# bench

A fixed scoreboard for the improvement pipeline: seeded bugs mined from this repo's own
fix commits, solved by the real cycle in a sandbox, and scored by tests the agent cannot
touch. Why and how: [PLAN.md](PLAN.md).

`bench/` is outside `allowed_modification_paths`, so the agent cannot edit the tasks, the
harness or the results. CI does not run it (CI runs `pytest tests/` only). It runs on
Elaeis, never on rubrum, from a checkout **outside `$HOME`**: the sandbox denies reads
under `$HOME`, so `run.py` refuses a checkout there.

## Run

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'

# 1. Build the task set (no LLM, ~2 min). Rewrites bench/tasks/.
.venv/bin/python bench/build_tasks.py

# 2. Controls (no LLM). Gold must be 100%, null 0%; otherwise the harness is broken.
.venv/bin/python bench/run.py --mode gold --split all
.venv/bin/python bench/run.py --mode null --split all

# 3. Score the pipeline at this checkout's HEAD. Uses production's OpenRouter key, which
#    the live agent shares. All bench use together stops at BENCH_DAILY_CAP requests/day
#    (default 300, ledger in bench/results/ledger.tsv).
export BW_SESSION="$(~/.agents/skills/custom/bitwarden-cli/scripts/bw-session.sh)"
export BENCH_LLM_API_KEY="$(bw get item 'OmniRoute / openrouter' | jq -r .notes | sed -n 's/^API_KEY=//p')"
.venv/bin/python bench/run.py --mode cycle --split dev --note "what changed"

# 4. The outer loop (PLAN.md, Phase 2): one step per invocation, same key and cap.
.venv/bin/python bench/outer.py                      # calibrate main, or screen one program.md edit
.venv/bin/python bench/outer.py --confirm <exp-id>   # one paired replicate, ~270 requests
.venv/bin/python bench/outer.py --heldout <exp-id> --open-pr
.venv/bin/python bench/habits.py                     # why the newest run missed what it missed

# Harness tests
.venv/bin/python -m pytest bench/tests -q
```

`--agent-src path/to/other/checkout/src` scores a different version of the pipeline
against the same tasks. That's how a candidate change gets compared with `main`.

## Read

`results.tsv` gets one row per run:

| column | meaning |
|---|---|
| `resolved_rate` | **the number**: tasks whose F2P and P2P tests all pass, divided by tasks scored |
| `false_merge` | the cycle ended `success` (production would open and auto-merge a PR) but the bug is not fixed |
| `broke_other` | tasks where tests outside the task's own files stopped passing |
| `scored` vs `n` | tasks cut short by the request budget, the daily quota or an auth error are `invalid` and excluded |
| `agent_sha`, `model_cfg`, `task_set` | what was measured; only compare rows where `task_set` matches |

Per-task details (cycle status, the agent's identified task, its diff, the worker log) go
in `bench/results/<run-id>/`, which is not tracked.

With ~29 dev tasks one task is ~3.5 points, and free models are noisy. Compare
versions on the same tasks over several runs. A single-run difference of one or two
tasks means nothing.

## Splits

Tasks are sorted by date and every third one is `heldout`. Decide on `dev`; check
`heldout` occasionally, to catch the dev set being overfit.

`dev` is further split for the outer loop. `smoke` is 10 fixed tasks
(`tasks/SMOKE.json`) used for screening. `confirm` is the other 19, where keep/discard is
decided, so the tasks that picked an edit never also judge it.

`experiments.tsv` gets one row per outer-loop step: the habit targeted, the hypothesis,
screen or paired-confirm numbers (wins, losses, McNemar p), and the decision.
