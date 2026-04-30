# Configuration Reference

Last updated: 2026-04-30 20:57 UTC

## Safety Config (`src/ouroboros/config.py`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `pr_only` | `True` | Only create PRs, never commit to default branch directly |
| `allow_network` | `True` | Allow network access during improvement |
| `allow_write_default_branch` | `False` | Allow writing directly to main/master |
| `require_human_approval` | `False` | Require human review before merge |
| `allow_self_modification` | `True` | Allow modifying own runtime config |
| `enable_auto_merge` | `True` | Auto-merge PRs when CI checks pass |
| `max_retry_on_failure` | `1` | Number of root-cause-analysis retries on test regression |
| `sandbox_enabled` | `False` |  |
| `sandbox_image` | `python:3.11-slim` |  |
| `reviewer_model` | `gpt-5.4-nano-2026-03-17` |  |
| `reviewer_base_url` | `None` |  |
| `reviewer_api_key` | `None` |  |
| `max_improvements_per_day` | `3` | Rate limit on daily improvement attempts |
| `max_changed_files_per_pr` | `3` | Max files per autonomous PR |
| `max_lines_changed_per_pr` | `200` | Max lines changed per autonomous PR |
| `allowed_modification_paths` | `('src/ouroboros/', 'tests/', 'docs/wiki/')` | Directories the agent can modify |
| `forbidden_modification_paths` | `('config.py', 'improvement.py', 'git_ops.py', 'evaluation.py', 'policies.py')` | Files that can never be modified |

## Runtime Files

The live runtime config does not come from the repo `config/` directory.

| Path | Purpose |
|------|---------|
| `~/.config/moltbook/agent.json` | Live runner configuration |
| `~/.config/moltbook/credentials.json` | Secrets and runtime credentials |
| `~/.config/moltbook/state.json` | Moltbook loop state |
| `~/.config/moltbook/self_improvement_state.json` | Scheduled self-improvement state |
| `~/.config/moltbook/knowledge_base.json` | Persisted feed insights |

`config/agent.json` in the repo is only a checked-in sample/reference file.

## Credentials (`~/.config/moltbook/credentials.json`)

Typical keys:

| Key | Purpose |
|-----|---------|
| `openai_api_key` | Required for OpenAI-backed LLM features |
| `api_key` | Moltbook API key for status/feed/posting |
| `agent_name` | Required for Moltbook features that inspect the agent's own posts |
| `telegram_bot_token` | Optional Telegram notifications secret |
| `telegram_chat_id` | Optional Telegram destination |

## Runner Config (`~/.config/moltbook/agent.json`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `interval_seconds` | `1800` | Main loop cycle interval |
| `enable_auto_comment` | `False` | Post comments on Moltbook posts |
| `dry_run` | `False` | Preview mode, no actual changes |
| `enable_telegram_notifications` | `False` | Send events to Telegram |
| `enable_auto_git_push` | `False` | Auto-commit and push state changes |
| `enable_self_improvement` | `False` | Enable autonomous code improvement |
| `improvement_interval_hours` | `48` | Hours between improvement attempts |
| `improvement_model` | `gpt-5.4-nano-2026-03-17` | LLM model for code generation |
| `enable_auto_merge` | `False` | Auto-merge PRs when checks pass |
| `enable_community_improvement` | `False` | Post improvements for community feedback |
| `enable_github_improvement` | `False` | Auto-fix GitHub issues |
| `github_improvement_interval_hours` | `12` | Hours between issue checks |