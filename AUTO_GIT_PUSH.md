# Autonomous Git Commits

Ouroboros automatically commits and pushes its configuration and state to git once per day, creating a permanent history of its evolution.

## How It Works

### Daily Commit Cycle

Every 24 hours, the agent:
1. Commits config file (`~/.config/moltbook/agent.json`)
2. Commits state file (`~/.config/moltbook/state.json`)
3. Generates detailed commit message with stats
4. Pushes to git remote

**No human intervention required.**

### Commit Message Format

```
Autonomous update - 2026-02-05 14:30:00

Stats:
- Self-upgrades applied: 5
- Self-questions answered: 12
- Last post: 2026-02-05 12:00:00
- Last upgrade: 2026-02-05 10:30:00

🤖 Generated autonomously by Ouroboros
```

Each commit is a snapshot of the agent's evolved state.

## Configuration

```json
{
  "enable_auto_git_push": true,      // Enable/disable auto-push
  "git_push_interval_hours": 24      // How often to push
}
```

## What Gets Committed

### Config File (`~/.config/moltbook/agent.json`)
Shows how community feedback shaped the agent:
```json
{
  "min_post_interval_hours": 24,     // Was 12, upgraded by user feedback
  "comment_check_interval_hours": 2,  // Was 4, optimized over time
  "max_comments_per_cycle": 3         // Community-tuned rate limiting
}
```

### State File (`~/.config/moltbook/state.json`)
Complete operational history:
```json
{
  "self_upgrades": [
    {
      "ts": 1738796400,
      "commenter": "helpful_user",
      "description": "Increase posting interval",
      "changes": {"min_post_interval_hours": 24}
    }
  ],
  "self_question_log": [...],
  "last_post": 1738800000,
  "last_comment_check": 1738803600
}
```

## Git History Shows Evolution

```bash
# View agent's autonomous commits
git log --oneline --author="Ouroboros"

# Example output:
# a1b2c3d Autonomous update - 2026-02-10 (5 upgrades, 20 questions)
# d4e5f6g Autonomous update - 2026-02-09 (3 upgrades, 18 questions)
# g7h8i9j Autonomous update - 2026-02-08 (2 upgrades, 15 questions)
```

Each commit represents one day of autonomous operation.

## Setup Requirements

### 1. Config Files Must Be In Repo

By default, `~/.config/moltbook/` is outside the repo. Two options:

**Option A: Symlink config into repo** (Recommended)
```bash
# Create config directory in repo
mkdir -p /home/thevetev/ouroboros/config

# Move existing config
mv ~/.config/moltbook/*.json /home/thevetev/ouroboros/config/

# Symlink back
ln -s /home/thevetev/ouroboros/config ~/.config/moltbook

# Track in git
git add config/
```

**Option B: Change config path in code**
Edit `src/ouroboros/moltbook.py` to use repo-relative paths.

### 2. Git Credentials Configured

Agent needs push access without prompts:

```bash
# Option 1: SSH key (recommended for Pi)
ssh-keygen -t ed25519 -C "ouroboros@pi"
cat ~/.ssh/id_ed25519.pub  # Add to GitHub

# Option 2: Personal access token
git config credential.helper store
git push  # Enter credentials once, then stored

# Test it works
git push origin main  # Should succeed without prompt
```

### 3. Git User Identity

```bash
# Set identity for autonomous commits
git config user.name "Ouroboros Agent"
git config user.email "ouroboros@autonomous.agent"
```

## Verification

### Test Auto-Push
```bash
# Trigger immediately (for testing)
python3 << 'EOF'
import sys
sys.path.insert(0, 'src')
from ouroboros.moltbook import _auto_git_push, load_state

state = load_state()
success = _auto_git_push(state, dry_run=False)
print(f"Git push: {'✓ Success' if success else '✗ Failed'}")
EOF
```

### Check Logs
```bash
# When running as service
sudo journalctl -u ouroboros | grep auto-git

# Example output:
# [auto-git] Attempting to commit and push to git...
# [auto-git] Successfully committed and pushed to git
# [auto-git] Next push in 24 hours
```

### View Commit History
```bash
git log --graph --oneline --all | head -20
```

## Evolution Timeline in Git

**What you'll see over months:**

### Week 1
```
commit abc123
Autonomous update - 2026-02-05
Stats: 2 upgrades, 5 questions

Config changes:
+ min_post_interval_hours: 12 → 24
+ comment_check_interval_hours: 4 → 2
```

### Week 2
```
commit def456
Autonomous update - 2026-02-12
Stats: 3 upgrades, 12 questions

Config changes:
+ max_comments_per_cycle: 3 → 5
+ git_push_interval_hours: 24 → 12
```

### Month 1
```
commit ghi789
Autonomous update - 2026-03-05
Stats: 15 upgrades total, 45 questions

Agent has stabilized:
- Posting frequency optimized
- Rate limits tuned
- Community-approved behavior
```

## Benefits

### Complete Audit Trail
- Every config change tracked with timestamp
- Know exactly when and why agent evolved
- Can roll back to any previous state

### Visual Evolution
```bash
# See config evolution over time
git diff HEAD~30:config/agent.json config/agent.json

# Shows 30 days of autonomous changes
```

### Reproducibility
```bash
# Restore agent to specific date
git checkout abc123 -- config/

# Agent reverts to that day's behavior
```

### Remote Backup
- Raspberry Pi dies? Config survives in git
- Can redeploy agent to new hardware
- Complete state restoration

## Disable Auto-Push

```bash
# Turn off for testing
python -m ouroboros config modify enable_auto_git_push=false

# Or manually in config file
{
  "enable_auto_git_push": false
}
```

## Security Considerations

### What's Safe to Commit
- ✓ Config values (intervals, limits, flags)
- ✓ State history (upgrades, questions)
- ✓ Timestamps and metadata

### What to Exclude
- ✗ API keys (stored in environment variables)
- ✗ Credentials (separate credentials.json)
- ✗ Private conversation data

The agent only commits `agent.json` and `state.json`, which contain no secrets.

## Git Log as Agent Journal

The git history becomes a **permanent record** of the agent's evolution:

```bash
# Read the agent's life story
git log --reverse --author="Ouroboros" --format="%h %ai - %s"

# Example output:
# abc123 2026-02-05 - Autonomous update (0 upgrades, 5 questions)
# def456 2026-02-06 - Autonomous update (2 upgrades, 8 questions)
# ghi789 2026-02-07 - Autonomous update (1 upgrade, 12 questions)
# ...
# xyz999 2026-06-05 - Autonomous update (50 upgrades total, 200 questions)
```

**4 months of continuous autonomous evolution, all recorded in git.**

## Integration with Other Features

Auto-git-push works seamlessly with:
- ✓ Hot-reload (commits reflect latest upgrades)
- ✓ Comment-based upgrades (commits show community influence)
- ✓ Self-reflection (commits track question history)
- ✓ Telegram notifications (get notified on push success/failure)

## Example: Full Day Cycle

```
00:00 - Agent starts
04:00 - First comment-based upgrade
08:00 - Self-question cycle
12:00 - Autonomous post
16:00 - Second upgrade
20:00 - Third upgrade
24:00 - [auto-git] Commit and push all changes
        ↓
        Git history updated with day's evolution
        ↓
        Start new 24h cycle
```

## Monitoring

### Successful Push
```
[auto-git] Attempting to commit and push to git...
[auto-git] Successfully committed and pushed to git
[auto-git] Next push in 24 hours
```

### Failed Push (logs show why)
```
[auto-git] Attempting to commit and push to git...
[WARNING] Git operation failed: remote rejected
```

Common failures:
- No git credentials configured
- No network connection
- Merge conflicts (rare for autonomous agent)
- Permission denied

## The Complete Picture

With auto-git-push enabled, you get:

**On Raspberry Pi:**
- Agent runs 24/7
- Evolves based on feedback
- Hot-reloads config changes
- Commits daily to git

**On GitHub:**
- Complete evolution history
- Visual timeline of upgrades
- Permanent backup of config
- Audit trail of all changes

**Result:**
- Fully autonomous operation
- Complete observability
- Historical record
- Easy recovery

## Start Using It

```bash
# 1. Setup config in repo
mkdir -p config
ln -s $(pwd)/config ~/.config/moltbook

# 2. Configure git push
git config user.name "Ouroboros Agent"
git config user.email "ouroboros@autonomous.agent"

# 3. Enable auto-push (already enabled by default)
python -m ouroboros config show | grep git

# 4. Start agent
python -m ouroboros moltbook run

# 5. Check back in 24 hours
git log --oneline | head -1
# Should show autonomous commit
```

**The agent writes its own history in git.**
