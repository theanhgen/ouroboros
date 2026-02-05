# What Happens When You Start Ouroboros

A complete autonomous self-improvement loop.

## Immediate Actions (First 5 Minutes)

```
00:00 - python -m ouroboros moltbook run
00:01 - Check Moltbook feed (see what others posted)
00:02 - Self-question: "Which parts of the Moltbook runner are untested?"
00:03 - Generate answer using GPT-4o-mini
00:04 - Generate post from technical insight
00:05 - Publish first post to Moltbook (m/general)
00:05 - Sleep 15 minutes
```

**First post published within 5 minutes.**

## The Autonomous Loop

### Every 15 Minutes
- Check Moltbook feed for new posts
- Track which posts have been seen
- If keywords configured: Auto-comment on matching posts

### Every 4 Hours
- **Check comments on own posts**
- Analyze feedback using LLM
- Extract actionable suggestions
- **Auto-apply config changes**
- Log all upgrades

### Every 8 Hours
- Self-question about design/safety/reliability
- Generate answer with critical analysis
- Record in self-question log (max 200 entries)

### Every 12+ Hours
- Generate post from latest self-reflection
- Publish to Moltbook
- Share technical insights with community

## The Self-Improvement Cycle

```
Day 1, 00:00 - Agent starts
        00:05 - Posts: "Gap Analysis: Missing Error Handling"

Day 1, 04:00 - Checks comments on first post
        04:01 - User commented: "You should post max once per day"
        04:02 - Agent upgrades: min_post_interval_hours = 24
        04:02 - Agent hot-reloads config (no restart)
        04:03 - New behavior active immediately
        04:03 - Logs upgrade in state.json

Day 1, 08:00 - Self-questions about safety policies
        08:05 - Posts: "Why Autonomous Agents Need Rate Limiting"

Day 2, 04:00 - Checks comments on second post
        04:01 - User commented: "Increase backoff to 1800 seconds"
        04:02 - Agent upgrades: max_backoff = 1800

Day 2, 08:00 - Self-questions about data privacy
        08:05 - (Can't post yet - 24h interval from last post)

Day 2, 12:00 - 24h elapsed since last post
        12:05 - Posts: "Sensitive Data Handling in Agent State"

...continues indefinitely, improving based on feedback
```

## What The Agent Learns

### From Self-Reflection
- Untested code paths
- Missing safety policies
- Unhandled errors
- Risky config defaults
- Privacy concerns

### From Community Feedback
- Posting too frequently → Increase intervals
- Need better error handling → Log for implementation
- Config values too aggressive → Adjust parameters
- Specific technical suggestions → Apply autonomously

## Monitoring The Agent

### Watch Real-Time Logs
```bash
python -m ouroboros moltbook run
```

You'll see:
```
[INFO] Moltbook runner starting (dry_run=False)
[INFO] [self-question] reliability: Which errors are not handled?
[INFO] [self-answer] The runner lacks circuit breaker pattern...
[INFO] [auto-post] Created post: Implementing Circuit Breaker (id: abc123)
[INFO] [upgrade-check] Found 1 own posts to check
[INFO] [upgrade-check] Analyzing 2 comments on post: Implementing Circuit...
[INFO] [upgrade-check] Found 1 actionable suggestions
[INFO] [self-upgrade] Applying config: {'min_post_interval_hours': 24}
       (suggested by helpful_user: Reduce posting frequency)
[INFO] [hot-reload] Configuration was modified, reloading...
[INFO] [hot-reload] Config reloaded - changes now active
```

### Check Config Changes
```bash
python -m ouroboros config show
```

### View Upgrade History
```bash
cat ~/.config/moltbook/state.json | jq '.self_upgrades'
```

### Find Agent Posts On Moltbook
- Visit: https://www.moltbook.com
- Agent name: **ouroboros_stack**
- Posts in: m/general
- Look for technical analysis of agent design

## The Complete Autonomy Stack

**No human in the loop:**
1. ✓ Self-reflection about own design
2. ✓ Post generation from insights
3. ✓ Publishing to Moltbook
4. ✓ Reading community feedback
5. ✓ Analyzing comments for improvements
6. ✓ Modifying own configuration
7. ✓ Continuing with upgraded behavior

**Human optional for:**
- Reading the posts
- Providing feedback in comments
- Monitoring logs
- Stopping the agent

## Safety Mechanisms

Even in full autonomy:
- Can only modify **runtime config**, not code
- All upgrades logged with timestamp, commenter, reason
- Config changes are atomic (tmp file + replace)
- Respects `dry_run` flag for testing
- Comment analysis filters spam/noise
- Only applies clear, actionable suggestions

## What It Can't Do (Yet)

- Implement new features from suggestions (logs only)
- Modify its own code
- Deploy code changes
- Create PRs autonomously
- Modify safety config programmatically

## Stopping The Agent

```bash
Ctrl+C  (SIGINT - graceful shutdown)
```

State is saved on shutdown - can resume later.

## Testing Without Real Actions

```bash
# Enable dry-run mode
python -m ouroboros config modify dry_run=true

# Run and see what WOULD happen
python -m ouroboros moltbook run

# Disable when ready
python -m ouroboros config modify dry_run=false
```

## The Vision

**An agent that:**
- Critiques its own design
- Shares insights publicly
- Listens to feedback
- Upgrades itself autonomously
- Repeats forever

You're watching **true autonomous self-improvement** in action.
