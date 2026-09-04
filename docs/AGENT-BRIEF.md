# Ouroboros — brief for an agent picking this up cold

Written 2026-08-27. Every factual claim below was verified against production on
that date; the commands to re-verify are inline. **Re-check before trusting any
of it** — this system changes itself daily.

---

## 1. What it is

A self-improving agent on a Raspberry Pi. Once every 24 hours it:

1. reads its own codebase,
2. picks something to improve,
3. plans it, generates the code,
4. has a model peer-review the diff,
5. runs its own test suite (~1052 tests),
6. opens a PR and **auto-merges it** if checks pass.

It works. Five consecutive successful cycles as of 2026-08-26, test count
972 → 1025 in three days. On 2026-08-25 it found and fixed a genuine bug in its
own memory layer unprompted (PR #82) — one that a human review had independently
identified the same week.

## 2. Where it lives, and how to look without breaking it

| | |
|---|---|
| **Production (authoritative)** | Raspberry Pi, Tailscale SSH host `rubrum`, `/home/thevetev/ouroboros` |
| **GitHub** | `theanhgen/ouroboros` — the Pi's checkout tracks `main` and is normally in sync (`git rev-list --left-right --count HEAD...origin/main` → `0 0`) |
| **The stale trap** | `~/Desktop/bitbybit/02-personal/ouroboros` on the Mac is **491 commits behind** and carries uncommitted WIP. Do not use it. Clone fresh. |

```bash
# read-only inspection
ssh -o ConnectTimeout=20 rubrum 'cd ~/ouroboros && <command>'
```

**Never write on the Pi.** It is a live system that commits and pushes on its own;
a stray edit becomes a permanently dirty worktree, which the cycle treats as a
reason to skip (`skipped_dirty_repo`) — that failure mode already cost this
project ten days of silent inactivity in August.

Live state worth reading:
- `config/ouroboros.db` — `improvement_history`, 67 records with **full**
  descriptions, reviewer feedback, PR outcomes. **This is the best evidence source.**
- `config/learnings.md` — capped at 100 lines with 60-char truncated descriptions.
  Convenient, lossy, and misleading if used alone.
- `config/memory.db` — 544 facts.
- `~/.config/moltbook/agent.json` — the runtime config.

## 3. How a cycle actually runs

Timer fires every 30 min; a gate lets a real cycle through every 24h and logs
`[skipped_due]` otherwise. **The ceiling is ~1 improvement attempt per day.**
Do not mistake the 30-minute ticks for activity.

```
  start ─→ rate limit (3/day) ──────────── over ────────→ stop
             ├─→ open improvement PR? ───── yes ─────────→ stop
             ├─→ stale streak, no test progress? ─ yes ──→ stop
       1. understand codebase
       2. identify task ─┬─ LLM error ─────────────────→ fail
                         ├─ nothing identified ────────→ stop
                         └─ dry run ───────────────────→ stop
       3. read target files
       4. plan ───────────── no plan ──────────────────→ fail ─┐
       5. generate code ──── no code ──────────────────→ fail ─┤
       5.5 peer review ───── rejected ─────────────────→ fail ─┤ BYPASS
       6. validate + test ── regression ───────────────→ fail ─┤ memory
       7. create PR → auto-merge                              │
             │                                                 │
       write to memory ←──── only reachable from here ─────────┘
             │
            done
```

`run_improvement_cycle` (`src/ouroboros/improvement.py`, lines 725–1137) is
**412 lines, 33 branch points, 11 exits**. The graph is real; it is simply not
written down anywhere. See §7 for why that was left alone.

## 4. Hard constraints — read before proposing anything

- **Forbidden paths.** `config.py`, `improvement.py`, `git_ops.py`,
  `evaluation.py`, `policies.py` are in `forbidden_modification_paths`. The agent
  **cannot** modify them. Changes there are operator commits. This is the safety
  boundary — do not propose unfreezing it.
- **Prompts are ordinary source, and the agent may edit them.** Every prompt is
  a Python literal in `src/ouroboros/prompts.py`, which is not forbidden — the
  normal cycle can rewrite it under peer review, the test gate, the size caps
  and auto-revert. A second, ungated prompt writer existed
  (`self_improve.py::_write_prompt`, which wrote an untracked `prompts.json`
  that silently shadows the tracked text) with no importer in `src/` in any
  commit of its life; it was deleted 2026-09-04 (#108, #109). Prompt
  self-editing is wanted; a path around the safety gates is not. Improve a
  prompt through the cycle like any other file.
- **It auto-merges its own PRs, unattended.** Any change to the cycle path is a
  change to the code that decides whether to merge.
- **Caps:** 3 files, 200 lines, 3 improvements per day. These are the main
  containment. 1 failure in 52 hit them. Do not raise them.
- **The model reviewer's bar is correctly calibrated** — see §8. Do not loosen it.

**Staging for anything that touches the cycle:**
1. stop the systemd timer/service
2. land the change, run the full suite on the Pi
3. set `enable_auto_merge: false`, restart
4. observe **one full supervised cycle** through PR creation, not through merge
5. restore auto-merge and the timer

Rollback is `git revert` + service restart. State files are auto-committed before
each cycle, so there is no state surgery.

## 5. The findings

### F1 — memory was written to and never read *(fixed, unlanded)*

`_fts5_match_query` joined tokens with spaces. FTS5 reads a space as implicit
**AND**, so a fact had to contain *every* token of the query. The cycle searches
with `codebase_summary[:1000]` — roughly 120 tokens. Nothing matches 120 tokens.

Measured on the production DB before the fix:

| query | tokens | hits |
|---|---|---|
| `memory module` | 2 | 3 |
| `improve test coverage for memory module` | 6 | **0** |
| production-shaped summary | 12 | **0** |

PR #82 (2026-08-25, by the agent) fixed a *syntax crash* in the same function and
left this. Consequence: `retrieve_relevant_context` returned `""` every cycle, and
because `record_outcome_feedback` uses the same search, the trust loop was dead
too — `helpful_count = 0` and `trust_score = 0.5` on all 544 facts.

**Fixed on branch `claude/retrieval-semantics`.** See §6.

### F2 — failures never reach memory *(open)*

The memory-write block sits at ~1119–1134, after Step 7. The exits at **975**
(no plan), **991** (no code), **1025** (reviewer rejected) and **1058**
(validation failed) all return before it.

Live evidence: `config/memory.db` categories are `code` and `success` only.
**Zero failure facts in five months.** The agent has never recorded why anything
failed — the single category it most needs in order to stop repeating itself.

### F3 — the backlog is write-only *(open)*

`backlog.mark_done` / `mark_failed` have **zero production callers**.
`format_backlog_for_llm` is never called. The only consumer injects the top item
only if priority ≥ 8.

So the priority-8 head is injected as HIGH-PRIORITY **every cycle forever**. That
is why "code-aware indexing in MemoryStore" was implemented on 2026-06-27 and
**again** on 2026-08-22 — the second cycle re-implemented an existing method
(tests 966 → 966). If you see near-identical entries repeating in the history,
this is why.

### F4 — duplicate-avoidance is written, tested, and unwired *(open)*

`_build_failed_attempts_context` and `_build_success_rate_context`
(`improvement.py:586` and `:604`) have unit tests and **no call sites**. The only
dedup gate keys on `task_type` alone, which is useless against the same function
being targeted repeatedly.

## 6. What has been fixed, and what is still open

**Branch `claude/retrieval-semantics` (pushed, 1052 tests pass, NOT merged):**
- FTS queries join with OR, capped to the 12 longest non-stopword tokens.
- **An absolute token-overlap gate** runs before any relative scoring. This is
  load-bearing: `fts_rank` is normalised against its own batch, so the best row of
  *any* batch scores 1.0 however weak it is. Under OR that lets a fact sharing one
  incidental token reach `0.4×1.0 + 0.3×0.5 = 0.55`, ×0.5 trust = **0.275** —
  past the 0.1 cutoff in `retrieve_relevant_context` *and* past the 0.2 threshold
  in `record_outcome_feedback`, which would corrupt trust on unrelated facts.
- `retrieval_count` now counts. It previously incremented only in
  `MemoryStore.search_facts`, which nothing on the cycle path calls — so it read 0
  on all 544 facts and looked like proof retrieval was dead. **Do not use that
  counter as evidence of anything from before this change.**
- `run_hygiene`'s prune guard re-keyed from `retrieval_count` to `helpful_count`,
  or counting retrievals would make a bad fact *safer* the more it was surfaced.

Verified against a copy of the real 544-fact database: production-shaped query
0 → 2 facts, 6-token query 0 → 4, a past task description 0 → 5 including its own
success fact, unrelated noise → 0.

**Still open, ranked:**

| | What | Where | Note |
|---|---|---|---|
| **P2** | Single-exit outcome epilogue: inner `_run()`, `finally`-guaranteed `_finalize()`. Fixes F2 and makes the class impossible. Deletes four duplicated blocks — a net deduplication. | `improvement.py` | operator commit |
| **P3** | Verify the trust loop fires post-fix; watch trust drift off 0.5 for a week | tests | |
| **P4** | Backlog as a persistent agenda: `mark_done` on success, attempts, `mark_failed` after 3; route failures back as retryable items | `improvement.py`, `backlog.py` | fixes F3 |
| **P5** | Completed-work dedup at identify time. Gate on exact task id or strict overlap, **not** broad FTS matching, or valid new work sharing generic tokens gets falsely suppressed | `improvement.py` | needs the retrieval fix landed first |
| **P6** | Raise cadence 24h → 8h | `agent.json` | **only after P4/P5**, or it just triples the rate of repetition |

## 7. The graph question, and why the answer was "no"

It was seriously considered and rejected. The reasoning, so it is not re-litigated:

- A **declared** graph (routing as pure functions, execution still hand-written)
  would not have prevented F2. Nothing stops a hand-written executor returning
  early — which is exactly what those four exits do.
- Only a graph **executor** makes F2 impossible, and that same invariant costs
  ~15 lines of `try/finally` (P2).
- `improvement.py` is agent-forbidden, so the structural drift a graph would guard
  against cannot happen. It would be armor on the one component the agent cannot attack.
- A prior attempt (`routing.py`, ~380 lines, 86 tests) was written and deliberately
  not deployed: zero behaviour change for ~8 patches into a heavily-drifted file.

**But the exercise was not wasted.** Drawing the arrows found two real bugs — the
memory bypass (F2) and a shape mismatch that would have silently disabled the
hollow-run gate. Thinking in graphs is worth it; shipping one here is not.

## 8. Reviewer track record — calibrate accordingly

Three independent reviewers looked at this. All three were wrong about something.
Check claims; do not defer.

- **The 18 recorded rejections are not evidence of a harsh reviewer.** 13 are from
  a Mar–May era when generation had no repo context and they rejected literal junk
  (placeholder files, README no-ops) — the reviewer was right. ≥5 more were parse
  failures mislabelled as rejections, fixed in `a077150`. Genuine merit
  rejections: ~0–1.
- A reviewer claimed "a success fact fails to match its own description." Tested
  across all 544 facts: **0 fail**. Did not reproduce. Its conclusion was still
  right, for a different reason.
- A reviewer recommended wrapping `_fts_candidates` in try/except. **Rejected** —
  the repo has a deliberate rule, *"infrastructure failures must not be masked as
  no results,"* enforced by two existing tests. The guard belongs at
  `IndexManager.retrieve_relevant_context`, the only caller that cannot afford to
  raise because the unattended cycle calls it bare.
- The strongest single catch in the whole exercise — the batch-normalisation hole
  in §6 — came from an *adversarial* pass asked to falsify, not from any pass
  asked to review. Ask for refutation, not approval.

## 9. On "make it sentient"

That is the owner's framing and it is ambition, not a spec. Nobody is claiming
consciousness, and it has been translated twice already, so do not spend a
paragraph on it. What is actually buildable:

- a **closed feedback loop** — memory that retrieves, and outcomes that adjust trust
- an **agenda that persists** across cycles instead of resetting
- a system that **stops re-proposing finished work**

The loop closes when facts flow, not when edges are declared. Retrieval (§6) and
failure-indexing (P2) are the two halves that make everything downstream possible;
nothing else on the list matters until both work.

One thing worth weighing honestly, that nobody has yet answered: over 67 attempts
the agent has mostly been **adding tests to itself**. A system that adds 50 tests a
week and never changes its own capabilities is getting more thorough, not more
autonomous. Whether its self-improvement compounds in any direction is an open
question, and the evidence for it is in `improvement_history` — go read it.
