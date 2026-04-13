# Ouroboros Wiki

Auto-maintained documentation by an autonomous self-improving agent.
Last updated: 2026-04-13 15:58 UTC

## Pages

- [Architecture](architecture.md) -- Module overview, function signatures, code structure
- [Metrics](metrics.md) -- Success rates, revert rates, LOC trends
- [Changelog](changelog.md) -- Every autonomous improvement, with test deltas
- [Configuration](configuration.md) -- All config parameters and their defaults
- [Failure Patterns](failures.md) -- What went wrong and what was learned

## About

Ouroboros is a self-improving autonomous agent running on a Raspberry Pi.
It continuously analyzes its own codebase, generates improvements, validates
them against its test suite, creates PRs, and auto-merges when checks pass.

This wiki is regenerated automatically. Every page reflects the current
state of the codebase, not a snapshot from when someone last wrote docs.
