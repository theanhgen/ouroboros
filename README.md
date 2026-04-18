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

## Documentation

All docs live in the [Wiki](https://github.com/theanhgen/ouroboros/wiki):

- [Architecture](https://github.com/theanhgen/ouroboros/wiki/architecture) -- modules, function signatures, code structure
- [Configuration](https://github.com/theanhgen/ouroboros/wiki/configuration) -- all parameters and defaults
- [Metrics](https://github.com/theanhgen/ouroboros/wiki/metrics) -- success rates, revert rates, trends
- [Changelog](https://github.com/theanhgen/ouroboros/wiki/changelog) -- every autonomous improvement
- [Failure Patterns](https://github.com/theanhgen/ouroboros/wiki/failures) -- what went wrong and what was learned

## Repository Layout

- `src/ouroboros/` -- core agent code
- `config/` -- checked-in sample config and repo-local snapshots, not the live runtime path
- `tests/` -- test suite
- `docs/wiki/` -- wiki source (auto-generated, pushed to GitHub Wiki)
