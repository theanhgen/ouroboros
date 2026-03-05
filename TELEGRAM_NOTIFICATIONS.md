# Telegram Notifications

Your Ouroboros agent sends real-time updates to your Telegram, so you can passively monitor autonomous operations from your phone.

## Current Configuration

```json
{
  "enable_telegram_notifications": true,
  "telegram_error_min_interval_seconds": 60
}
```

**Status**: ✅ Enabled and configured

## What You'll Receive

### Startup/Shutdown
```
🤖 Moltbook runner started (dry_run=False)
```
```
🤖 Moltbook runner stopped.
```

### Autonomous Posts
```
📝 Post created: Implementing Circuit Breaker for Enhanced Network Reliability (id: abc123)
```

### Auto-Comments
```
💬 Commented on post abc123: What Task Should Agents Automate?
```

### Self-Questions
```
❓ Self-question [reliability]: Which errors are not handled in Moltbook API requests?

Answer: The runner lacks circuit breaker pattern for network failures. Currently only basic exception catching exists with exponential backoff, but there's no protection against repeated failed API calls during extended outages...
```

### Self-Upgrades (Most Important!)
```
⚡ Applied config change from comment by helpful_user: Increase posting interval to reduce spam
Changes: {'min_post_interval_hours': 24}
```

### Git Commits
```
📦 Auto-git push succeeded. Next push in 24h.
```

### Errors (Rate-Limited)
```
❌ Error in run_loop cycle (1 consecutive). Backing off 30s.
```
```
❌ Auto-git push failed.
```
```
❌ Error during comment-based upgrade check
```

*Error notifications are rate-limited to once per 60 seconds to prevent spam*

## Notification Flow

### Typical Day
```
00:00 🤖 Runner started
00:05 📝 Post created: Gap Analysis...
04:00 💬 Commented on post xyz789
08:00 ❓ Self-question [safety]: What safety policy is missing...
12:00 📝 Post created: Why Autonomous Agents...
16:00 ⚡ Applied config change from user_123: {'min_post_interval_hours': 24}
20:00 ❓ Self-question [privacy]: What data do we store...
24:00 📦 Auto-git push succeeded
```

**You see the agent's life unfold in real-time on your phone.**

## Error Notifications

### When Errors Occur
- First error: Immediate notification
- Subsequent errors: Once per 60 seconds max
- Prevents notification spam during outages

### Common Errors
```
❌ Error in run_loop cycle (3 consecutive). Backing off 180s.
```
*Usually network issues, agent auto-recovers*

```
❌ Auto-git push failed.
```
*Git credentials issue or network problem*

```
❌ Failed to create autonomous post
```
*OpenAI API issue or rate limit*

## Configuration

### Enable/Disable
```bash
# Enable
python -m ouroboros config modify enable_telegram_notifications=true

# Disable (stops all notifications)
python -m ouroboros config modify enable_telegram_notifications=false
```

### Change Error Rate Limit
```bash
# More frequent error updates (careful, can spam)
python -m ouroboros config modify telegram_error_min_interval_seconds=30

# Less frequent (default was 300, you set to 60)
python -m ouroboros config modify telegram_error_min_interval_seconds=120
```

### Update Bot Token/Chat ID
```bash
# Preferred: environment variables
export TELEGRAM_BOT_TOKEN=NEW_TOKEN
export TELEGRAM_CHAT_ID=NEW_CHAT_ID

# Or store in ~/.config/moltbook/credentials.json (untracked):
# {
#   "telegram_bot_token": "NEW_TOKEN",
#   "telegram_chat_id": "NEW_CHAT_ID"
# }
```

## What You DON'T Receive

To avoid spam, notifications are NOT sent for:
- Feed checks (every 15 minutes - too noisy)
- Cycle sleep messages
- Routine state saves
- Successful comment checks with no comments
- Debug-level operations

**Only significant events are notified.**

## Mobile Monitoring

With Telegram notifications, you can:

### ✅ Passive Monitoring
- Glance at phone occasionally
- See agent is running healthy
- Notice when upgrades happen
- Spot errors quickly

### ✅ No Action Required
- Notifications are informational
- Agent handles everything autonomously
- You don't need to respond
- Just watch evolution happen

### ✅ Remote Oversight
- Monitor from anywhere
- Don't need to SSH into Pi
- Don't need to check logs
- Real-time status updates

## Example Week of Notifications

### Day 1
```
09:00 🤖 Runner started
09:05 📝 Post created: Missing Error Handling...
13:00 ❓ Self-question [reliability]...
21:00 📝 Post created: Safety Policies for...
```

### Day 2
```
01:00 📦 Auto-git push succeeded
04:00 ⚡ Applied config: {'min_post_interval_hours': 24}
    (from user: helpful_user)
08:00 ❓ Self-question [privacy]...
```

### Day 3
```
01:00 📦 Auto-git push succeeded
09:00 📝 Post created: Data Privacy in Agent State
13:00 💬 Commented on post xyz: Great analysis...
16:00 ⚡ Applied config: {'comment_check_interval_hours': 2}
```

**Each day shows autonomous progress.**

## Integration with Other Features

Telegram works seamlessly with:
- ✅ Hot-reload (notified when config reloaded)
- ✅ Self-upgrades (notified of each upgrade)
- ✅ Git push (notified on success/failure)
- ✅ Error recovery (notified but agent auto-recovers)

## Telegram Bot Setup (Already Done)

Your bot is already configured, but for reference:

### 1. Created Bot
- Messaged @BotFather
- Created bot with `/newbot`
- Saved token

### 2. Got Chat ID
- Messaged @userinfobot
- Got chat_id: `454852478`

### 3. Configured Agent
- Added Telegram secrets to `~/.config/moltbook/credentials.json`
- Enabled notifications

**Status**: ✅ Complete and working

## Privacy & Security

### What's Sent
- Public post titles (already on Moltbook)
- Config changes (no secrets)
- Error messages (no API keys)
- Operational status

### What's NOT Sent
- API keys
- Full post content (only titles)
- Internal state data
- Debug information

### Bot Token Security
- Stored in env vars or credentials file (not tracked in git)
- Masked when displaying config
- Only used for outbound messages
- No inbound command handling

## Testing Notifications

### Test Immediately
```bash
# Restart agent to get startup notification
sudo systemctl restart ouroboros

# Should receive:
# 🤖 Moltbook runner started (dry_run=False)
```

### Trigger Upgrade Notification
```bash
# Manually modify config
python -m ouroboros config modify max_comments_per_cycle=10

# On next upgrade cycle, you'll see:
# ⚡ Applied config change...
```

## Disable Temporarily

```bash
# Turn off for quiet period
python -m ouroboros config modify enable_telegram_notifications=false

# Re-enable later
python -m ouroboros config modify enable_telegram_notifications=true
```

## The Complete Picture

**On Raspberry Pi:**
- Agent runs autonomously
- Makes decisions
- Evolves behavior

**On Your Phone:**
- Passive notifications
- Real-time status
- No action needed

**Result:**
- Complete oversight
- Zero intervention
- Just watch it evolve

## Summary

Telegram notifications give you:
- Real-time visibility into autonomous operations
- Instant awareness of self-upgrades
- Error alerts (but agent auto-recovers)
- Remote monitoring without SSH
- Passive oversight requiring no response

**Your agent talks to you, but doesn't need you to talk back.**

Perfect for watching autonomous evolution unfold.
