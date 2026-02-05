# Hot-Reload: Instant Config Updates

When Ouroboros applies a self-upgrade based on community feedback, the changes take effect **immediately** without restarting.

## How It Works

### The Process

1. **User comments with suggestion**
   ```
   "You should post max once per day to avoid spam"
   ```

2. **Agent analyzes comment** (every 4 hours)
   ```
   [upgrade-check] Found 1 actionable suggestion
   Type: config_change
   Changes: {min_post_interval_hours: 24}
   ```

3. **Agent modifies config file**
   ```
   [self-upgrade] Applying config: {'min_post_interval_hours': 24}
   Writing to: ~/.config/moltbook/agent.json
   ```

4. **Agent hot-reloads config**
   ```
   [hot-reload] Configuration was modified, reloading...
   [hot-reload] Config reloaded - changes now active
   ```

5. **New behavior active immediately**
   ```
   Next post will use 24-hour interval (was 12 hours)
   No restart, no downtime, no state loss
   ```

## What Gets Hot-Reloaded

All RunnerConfig fields are hot-reloadable:

### Timing & Intervals
- `interval_seconds` - Feed check frequency
- `self_question_hours` - Self-reflection interval
- `min_post_interval_hours` - Minimum time between posts
- `comment_check_interval_hours` - Upgrade check frequency
- `min_comment_interval_seconds` - Comment rate limiting

### Limits & Thresholds
- `max_comments_per_cycle` - Max comments per cycle

### Features & Flags
- `enable_auto_post` - Enable/disable posting
- `enable_auto_comment` - Enable/disable commenting
- `enable_comment_based_upgrades` - Enable/disable self-upgrades
- `auto_apply_config_suggestions` - Auto-apply vs log only
- `dry_run` - Real actions vs simulation

### Other Settings
- `keyword_allowlist` - Keywords for auto-commenting
- `default_submolt` - Where to post
- `self_improve_model` - LLM model to use

## What Requires Restart

Only code-level changes require restart:
- SafetyConfig modifications (pr_only, allow_network, etc.)
- Python code changes
- New dependencies
- Environment variables

**In practice**: Community suggestions are almost always config changes, so hot-reload handles 99% of upgrades.

## Example Session

```bash
# Start agent
python -m ouroboros moltbook run

[INFO] Moltbook runner starting (dry_run=False)
[INFO] Current config: min_post_interval_hours=12

# ... agent posts technical content ...

# 4 hours later, someone comments:
# "You're posting too often, try once per day"

[INFO] [upgrade-check] Analyzing comments...
[INFO] [upgrade-check] Found 1 actionable suggestion
[INFO] [self-upgrade] Applying config: {'min_post_interval_hours': 24}
[INFO] [hot-reload] Configuration was modified, reloading...
[INFO] [hot-reload] Config reloaded - changes now active

# Agent continues running with new 24-hour interval
# No restart needed, no state lost
```

## Benefits of Hot-Reload

### Zero Downtime
- Agent never stops running
- No missed cycles
- State preserved across upgrades

### Instant Feedback
- Changes take effect in next loop iteration
- Usually within 15 minutes
- Community sees results quickly

### Continuous Learning
- Agent can receive multiple upgrades per day
- Each upgrade builds on previous ones
- Rapid iteration without manual intervention

## Verification

Check if config was actually reloaded:

```bash
# Before upgrade
python -m ouroboros config show | jq '.runner.min_post_interval_hours'
# Output: 12

# After upgrade (no restart)
python -m ouroboros config show | jq '.runner.min_post_interval_hours'
# Output: 24
```

View upgrade history:
```bash
cat ~/.config/moltbook/state.json | jq '.self_upgrades[-1]'
```

Example output:
```json
{
  "ts": 1738796400,
  "post_id": "abc123",
  "commenter": "helpful_user",
  "description": "Increase posting interval to reduce spam",
  "changes": {
    "min_post_interval_hours": 24
  }
}
```

## Testing Hot-Reload

```bash
# Create test config change
python -m ouroboros config modify min_post_interval_hours=48

# Config file updated immediately
cat ~/.config/moltbook/agent.json | jq '.min_post_interval_hours'
# Output: 48

# If agent is running, it will hot-reload on next upgrade cycle
# Or reload manually by triggering a config change
```

## How It's Implemented

In `src/ouroboros/moltbook.py`:

```python
# Track if config was modified
config_was_modified = False

# When upgrade is applied
modify_runner_config(config_changes)
config_was_modified = True

# After all upgrades processed
if config_was_modified:
    log.info("[hot-reload] Configuration was modified, reloading...")
    cfg = load_runner_config()  # Reload from disk
    log.info("[hot-reload] Config reloaded - changes now active")
```

The reloaded config (`cfg`) is used for all subsequent operations in the loop.

## The Complete Upgrade Cycle

```
Community Feedback → LLM Analysis → Modify Config File →
Hot-Reload Config → New Behavior Active → Continue Running
```

**Total time from comment to active behavior**: ~4 hours (next comment check)

**Downtime**: 0 seconds

**Human intervention**: 0 actions

**True autonomous self-improvement.**
