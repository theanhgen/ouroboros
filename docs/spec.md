# Product Spec: Ouroboros

## Summary
Ouroboros is a PR-only agent system that answers code questions and improves its own codebase over time. It uses evidence-linked answers, agent collaboration, and a strict safety model that only allows changes via pull requests.

## Problem
- Developer Q&A is fragmented and slow.
- LLM answers are fast but unreliable without grounding.
- Self-improving systems are risky without governance.

## Goals
- Provide grounded answers with citations to code/docs/tests.
- Use multiple agents to generate and critique solutions.
- Ship improvements only via PRs.

## Non-Goals (MVP)
- Autonomous merge to default branch.
- Unbounded spending or third-party publishing.
- Multi-org deployment.

## Users
- Developer maintaining a codebase.
- Agent operators who can supply specialized agents.

## Success Metrics
- Evidence rate: 90% of answers include citations.
- Acceptance: 80% of PRs are accepted or partially accepted.
- Cycle time: <24 hours from question to PR.

## Scope (MVP)
- Single repo.
- Read-only analysis plus PR creation.
- Human approval required to open PR.

## Core Workflow
1. Question intake and constraint parsing.
2. Retrieval from local code and docs.
3. Agent swarm produces candidate answers.
4. Synthesis + evidence scoring.
5. Patch proposal and PR draft.
6. Human approves PR creation.

## Safety Model
- Default branch is read-only.
- Changes only via PRs.
- Secrets scanning on diffs.
- Explicit allowlist for external tools.

## Open Questions
- Which repo is the first target?
- Which agents are available and trusted?
- What evidence rubric should be used?
