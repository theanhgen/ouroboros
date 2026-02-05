# Ouroboros

Ouroboros is a fully autonomous, self-improving agent system that can modify its own configuration and behavior without human review.

## Principles
- Full autonomy: agent can change config and behavior without human approval.
- Self-modification enabled: can update its own runtime configuration.
- Direct writes allowed: can write to default branch when configured.
- Evidence-first answers (citations to code/docs/tests).
- Reproducible runs with logged inputs and outputs.

## Status
Early scaffold. See `docs/spec.md` and `docs/architecture.md`.

## Autonomous Mode

Ouroboros runs in **full autonomy mode** by default with complete self-improvement capabilities:

### Core Autonomy
- `require_human_approval = False` - No human approval required for actions
- `allow_write_default_branch = True` - Can write directly to main/master
- `allow_network = True` - Network access enabled
- `allow_self_modification = True` - Can modify its own configuration
- `dry_run = False` - Actions are executed (not simulated)

### Autonomous Actions
- `enable_auto_post = True` - Posts technical insights from self-reflection
- `enable_auto_comment = True` - Comments on posts matching keywords
- `enable_comment_based_upgrades = True` - **Reads feedback and upgrades itself**
- `auto_apply_config_suggestions = True` - **Autonomously applies config changes**

### The Feedback Loop
1. Agent posts technical content about its own design
2. Community provides feedback in comments
3. Agent reads comments every 4 hours
4. LLM analyzes feedback for actionable suggestions
5. Agent autonomously modifies its config based on feedback
6. Agent continues improved behavior

See **COMMENT_BASED_UPGRADES.md** for complete documentation.

## Quick Start

View current configuration:
```bash
python -m ouroboros config show
```

Modify configuration autonomously:
```bash
python -m ouroboros config modify dry_run=true
python -m ouroboros config modify interval_seconds=3600
```

Check operational status:
```bash
python -m ouroboros plan
```

Run autonomous Moltbook loop:
```bash
python -m ouroboros moltbook run
```

## Repository Layout
- `docs/` design docs
- `src/ouroboros/` core code
- `tests/` tests (placeholder)
