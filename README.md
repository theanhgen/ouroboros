# Ouroboros

Ouroboros is a PR-only, self-improving agent system that answers code questions and proposes changes via pull requests. It never writes directly to `main` or `master`.

## Principles
- PR-only changes (no direct writes to default branch).
- Evidence-first answers (citations to code/docs/tests).
- Read-only by default; write actions are gated.
- Reproducible runs with logged inputs and outputs.

## Status
Early scaffold. See `docs/spec.md` and `docs/architecture.md`.

## Quick Start
```bash
python -m ouroboros.cli --help
```

## Repository Layout
- `docs/` design docs
- `src/ouroboros/` core code
- `tests/` tests (placeholder)
