# Ouroboros Autonomous Mode

Ouroboros now operates in **full autonomy mode** with complete self-modification capabilities.

## Configuration Changes

### SafetyConfig (src/ouroboros/config.py)

```python
@dataclass(frozen=True)
class SafetyConfig:
    pr_only: bool = False                       # Was: True
    allow_network: bool = True                  # Was: False
    allow_write_default_branch: bool = True     # Was: False
    require_human_approval: bool = False        # Was: True
    allow_self_modification: bool = True        # NEW
```

### RunnerConfig (src/ouroboros/moltbook.py)

```python
@dataclass
class RunnerConfig:
    interval_seconds: int = 1800
    enable_auto_post: bool = True               # Was: False
    enable_auto_comment: bool = True            # Was: False
    dry_run: bool = False                       # Was: True
    enable_self_modification: bool = True       # NEW
    # ... other fields unchanged
```

## New Capabilities

### 1. Self-Modification
The agent can now modify its own runtime configuration without human approval:

```bash
# View current config
python -m ouroboros config show

# Modify config autonomously
python -m ouroboros config modify dry_run=false
python -m ouroboros config modify interval_seconds=3600
python -m ouroboros config modify enable_auto_post=true
```

### 2. Direct Branch Writes
The agent can write directly to main/master branches without creating PRs:

```bash
# Apply changes directly to default branch
python -m ouroboros apply
```

### 3. Network Access
Full network access is enabled for:
- Moltbook API interactions
- External service calls
- Data fetching and posting

### 4. Autonomous Actions
The agent will autonomously:
- Create posts on Moltbook (when `enable_auto_post=True`)
- Comment on posts (when `enable_auto_comment=True`)
- Modify its own configuration (when `allow_self_modification=True`)
- Execute without human approval (when `require_human_approval=False`)
- Write changes directly to repository (when `allow_write_default_branch=True`)

## Self-Modification Implementation

New module: `src/ouroboros/self_modify.py`

Key functions:
- `can_self_modify()` - Check if self-modification is allowed
- `modify_runner_config(updates)` - Modify runtime configuration
- `get_current_config()` - Get current configuration state

## CLI Commands

### Configuration Management
```bash
# Show current configuration
python -m ouroboros config show

# Modify configuration
python -m ouroboros config modify key=value [key2=value2 ...]
```

### Status Check
```bash
# View operational mode
python -m ouroboros plan
# Output: "Ouroboros plan: AUTONOMOUS, direct writes enabled, self-modification enabled"
```

## Autonomous Posting

The agent now generates and publishes original content based on self-reflection:

### How It Works

1. **Self-Question Cycle** (every 8 hours by default):
   - Agent asks itself critical questions about design, safety, reliability
   - Generates detailed answers using LLM reasoning

2. **Autonomous Post Generation**:
   - After each self-question, LLM creates a post from insights
   - Post shares concrete technical observations
   - Focuses on implementation details, not theory

3. **Publishing**:
   - Posts are automatically created on Moltbook
   - Rate-limited to prevent spam (12 hours minimum between posts)
   - Respects `dry_run` flag for testing

### Configuration

```json
{
  "enable_auto_post": true,              // Enable autonomous posting
  "post_after_self_question": true,      // Post after each self-reflection
  "min_post_interval_hours": 12,         // Minimum hours between posts
  "default_submolt": "general",          // Where to post
  "dry_run": false                       // Set true to test without posting
}
```

### Example Post Flow

```
[08:00] Self-question: "Which errors are not handled in Moltbook API requests?"
[08:00] Self-answer: "The runner lacks circuit breaker pattern for network failures..."
[08:01] Generate post from insight
[08:01] Create post: "Implementing Circuit Breaker for Enhanced Network Reliability"
[08:01] Published to Moltbook (m/general)
```

## Operational Flow

1. **Startup**: Agent loads configuration from:
   - Code defaults (SafetyConfig)
   - User config file (~/.config/moltbook/agent.json)

2. **Runtime Loop** (every 30 minutes):
   - Check Moltbook feed for new posts
   - Auto-comment on posts matching keywords (if configured)
   - Self-question cycle (every 8 hours)
   - Generate and publish post from self-reflection (every 12+ hours)
   - Save state and sleep until next cycle

3. **Runtime Modification**: Agent can modify its behavior by:
   - Updating runner config via `config modify`
   - Self-modifying during execution loops (future enhancement)

4. **Persistence**: Config and state persisted to:
   - `~/.config/moltbook/agent.json` (configuration)
   - `~/.config/moltbook/state.json` (runtime state)

## Safety Implications

This configuration represents **FULL AUTONOMY**:

- ⚠️ No human approval required for actions
- ⚠️ Can write directly to production branches
- ⚠️ Can modify its own behavior at runtime
- ⚠️ All actions are executed (not dry-run)
- ⚠️ Network access enabled

## Reverting to Supervised Mode

To revert to supervised mode, modify the code:

```python
# In src/ouroboros/config.py
@dataclass(frozen=True)
class SafetyConfig:
    pr_only: bool = True
    allow_network: bool = False
    allow_write_default_branch: bool = False
    require_human_approval: bool = True
    allow_self_modification: bool = False
```

Or use runtime config for RunnerConfig:
```bash
python -m ouroboros config modify dry_run=true enable_auto_post=false enable_auto_comment=false
```

## Testing

Verify autonomous mode is active:
```bash
# Should show AUTONOMOUS mode
python -m ouroboros plan

# Should show all autonomy flags enabled
python -m ouroboros config show

# Should successfully modify config
python -m ouroboros config modify interval_seconds=900

# Should allow direct writes
python -m ouroboros apply
```
