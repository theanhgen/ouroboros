# Ouroboros

A fully autonomous, self-improving agent running on a Raspberry Pi. It continuously analyzes its own codebase, generates improvements, validates them against tests, and auto-merges when checks pass.

## Quick Start

```bash
git clone git@github.com:theanhgen/ouroboros.git
cd ouroboros
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Configure credentials
mkdir -p ~/.config/moltbook
# Create ~/.config/moltbook/credentials.json with:
# {
#   "openai_api_key": "sk-...",
#   "api_key": "moltbook-api-key",
#   "agent_name": "your-agent-name"
# }
# `api_key` and `agent_name` are only needed for Moltbook API features.
# Runtime config lives in ~/.config/moltbook/agent.json.

# Run
python -m ouroboros moltbook run
```

## How it runs in production

Two systemd units, and it matters which is which:

- **`ouroboros-self-improve.timer`** fires every 30 min. Most ticks log
  `[skipped_due]` and exit -- a real cycle only runs when
  `improvement_interval_hours` has elapsed. Seeing 30-minute ticks in the
  journal is not activity.
- **`ouroboros-moltbook.service`** is the long-running loop, and it is also
  **the deployment mechanism**: it polls git every 60s and calls `os._exit(0)`
  on a source change so systemd (`Restart=always`) relaunches it on the new
  code. Stopping it does not just pause the loop, it stops the agent picking up
  anything you merge.

A cycle: read the codebase, pick an improvement, plan it, generate code, have a
model peer-review the diff, run the full suite, open a PR, and auto-merge if
checks pass.

### Guards worth knowing before you touch anything

- **Dirty worktree aborts the cycle** (`skipped_dirty_repo`). Volatile state
  files listed in `_AUTO_STATE_FILES` (`git_ops.py`) are auto-committed first;
  anything else you leave modified blocks every cycle until it is committed.
- **`config/agent.json` is NOT one of those files**, and see the layout note
  below -- editing it is the most common way to accidentally wedge the agent.
- **An open improvement PR blocks the next cycle.** Only one is in flight at a
  time, by design.
- **`forbidden_modification_paths`** -- `config.py`, `improvement.py`,
  `git_ops.py`, `evaluation.py`, `policies.py`. The agent cannot modify these;
  changes there are operator commits.
- **Caps**: 3 files and 200 lines per change, and
  `max_improvements_per_day` attempts in a rolling 24h window.

## Documentation

All docs live in the [Wiki](https://github.com/theanhgen/ouroboros/wiki):

- [Architecture](https://github.com/theanhgen/ouroboros/wiki/architecture) -- modules, function signatures, code structure
- [Configuration](https://github.com/theanhgen/ouroboros/wiki/configuration) -- all parameters and defaults
- [Metrics](https://github.com/theanhgen/ouroboros/wiki/metrics) -- success rates, revert rates, trends
- [Changelog](https://github.com/theanhgen/ouroboros/wiki/changelog) -- every autonomous improvement
- [Failure Patterns](https://github.com/theanhgen/ouroboros/wiki/failures) -- what went wrong and what was learned

## Repository Layout

- `src/ouroboros/` -- core agent code
- `config/` -- **live runtime state on the Pi, not a sample.**
  `~/.config/moltbook/` resolves into this directory, so `agent.json`,
  `state.json`, `backlog.json`, `learnings.md` and the SQLite databases here
  are the ones the running agent reads and writes. Editing the config through
  either path writes into the working tree, and since `agent.json` is not an
  auto-committed state file, an uncommitted edit blocks every cycle with
  `skipped_dirty_repo`. Commit config changes.
- `tests/` -- test suite
- `docs/wiki/` -- wiki source (auto-generated, pushed to GitHub Wiki)
- `docs/AGENT-BRIEF.md` -- everything about this system that is easy to get
  wrong, with the commands to re-verify each claim. Read it before making
  changes.
