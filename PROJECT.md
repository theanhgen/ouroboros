---
stage: maintaining
---

Self-improving autonomous agent on Raspberry Pi — iteratively commits improvements to its own codebase using Claude.

## Current
The README presents Ouroboros as a running autonomous agent with a stable Raspberry Pi deployment model and most detailed documentation moved to the GitHub wiki. Top-level docs do not show the current live health or recent improvement cadence.

## Next
Run the Pi health check and record any drift between the live agent and the documented setup.

## Milestone
**What:** Pi health check + drift audit
**Target:** 2026-05-31
- [ ] SSH to Pi and confirm last successful run
- [ ] Pull metrics: success rate, revert rate over last 30 days
- [ ] Diff live config against repo defaults
- [ ] Check failure-pattern docs are up to date
