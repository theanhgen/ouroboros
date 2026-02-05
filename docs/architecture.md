# Architecture

## Components
- **Ingest**: normalizes questions and constraints.
- **Retrieve**: indexes repo + docs, returns evidence snippets.
- **Agents**: produce candidate answers and patches.
- **Synthesize**: merges, ranks, and scores evidence.
- **Diff/PR**: produces patch and PR draft.
- **Audit**: logs actions and provenance.

## Data Flow
Question -> Retrieve -> Agent swarm -> Synthesize -> Patch -> PR draft -> Human approval

## Evidence Format
- File path + line numbers
- Doc URL or local file path
- Test or lint output

## Safety Gates
- No writes to default branch.
- PR-only changes.
- Secrets scan on diff.
- External network calls disabled by default.

## Future
- Agent reputation scoring.
- CI-driven answer validation.
- Multi-repo support.
