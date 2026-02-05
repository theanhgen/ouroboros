# Deploying Ouroboros on Raspberry Pi

Complete guide for running the fully autonomous self-evolving agent on a Raspberry Pi.

## What You're Deploying

**A fully autonomous agent that:**
- Posts technical insights every 12-24 hours
- Reads community feedback on its own posts
- Upgrades itself based on suggestions
- Hot-reloads config without restarting
- Commits daily evolution to git
- Runs indefinitely, 24/7
- Sends Telegram notifications (optional)

**Zero human intervention required.**

## Prerequisites

### Hardware
- Raspberry Pi (3B+ or newer recommended)
- SD card (16GB minimum)
- Network connection (WiFi or Ethernet)
- Power supply

### Accounts
- Moltbook account (agent claimed)
- GitHub account (for git push)
- OpenAI API key (for LLM)
- Telegram bot (optional, for notifications)

## Installation Steps

### 1. Clone Repository

```bash
cd ~
git clone https://github.com/YOUR_USERNAME/ouroboros.git
cd ouroboros
```

### 2. Install Dependencies

```bash
# Install Python 3.10+
sudo apt update
sudo apt install python3 python3-pip python3-venv

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install openai>=1.0.0
```

### 3. Configure Credentials

```bash
# Create config directory
mkdir -p ~/.config/moltbook

# Moltbook credentials
cat > ~/.config/moltbook/credentials.json << 'EOF'
{
  "api_key": "your-moltbook-api-key",
  "agent_name": "ouroboros_stack"
}
EOF

# OpenAI API key
cat > ~/.config/moltbook/openai.json << 'EOF'
{
  "api_key": "sk-your-openai-key"
}
EOF

# Optional: Telegram bot
cat > ~/.config/moltbook/telegram.json << 'EOF'
{
  "bot_token": "your-telegram-bot-token",
  "chat_id": "your-chat-id"
}
EOF
```

### 4. Setup Config Tracking in Git

```bash
# Create config directory in repo
mkdir -p config

# Move config files into repo
mv ~/.config/moltbook/*.json config/

# Symlink back
rm -rf ~/.config/moltbook
ln -s ~/ouroboros/config ~/.config/moltbook

# Verify symlink
ls -la ~/.config/moltbook
# Should show -> ~/ouroboros/config
```

### 5. Configure Git Push

```bash
# Set git identity
git config user.name "Ouroboros Agent"
git config user.email "ouroboros@autonomous.agent"

# Setup SSH key for GitHub (recommended)
ssh-keygen -t ed25519 -C "ouroboros@raspberrypi"
cat ~/.ssh/id_ed25519.pub
# Copy and add to GitHub: Settings > SSH Keys

# Test git push works
git remote set-url origin git@github.com:YOUR_USERNAME/ouroboros.git
git push origin main
# Should succeed without password prompt
```

### 6. Configure Agent Settings

```bash
# Create initial config (optional - defaults are good)
cat > config/agent.json << 'EOF'
{
  "interval_seconds": 900,
  "enable_auto_post": true,
  "enable_auto_comment": false,
  "dry_run": false,
  "self_question_hours": 8,
  "min_post_interval_hours": 12,
  "enable_comment_based_upgrades": true,
  "comment_check_interval_hours": 4,
  "auto_apply_config_suggestions": true,
  "enable_auto_git_push": true,
  "git_push_interval_hours": 24,
  "enable_telegram_notifications": false
}
EOF
```

### 7. Test Run

```bash
# Activate venv
source .venv/bin/activate

# Test agent starts correctly
python -m ouroboros.cli moltbook status
# Should show: "status": "claimed"

# Dry run test (no real actions)
python -m ouroboros.cli config modify dry_run=true
python -m ouroboros.cli moltbook run
# Watch logs, press Ctrl+C after a minute

# Enable real mode
python -m ouroboros.cli config modify dry_run=false
```

## Production Deployment (Systemd Service)

### 1. Create Service File

```bash
sudo tee /etc/systemd/system/ouroboros.service << 'EOF'
[Unit]
Description=Ouroboros Self-Evolving Autonomous Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/ouroboros
ExecStart=/home/pi/ouroboros/.venv/bin/python -m ouroboros.cli moltbook run
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

# Prevent OOM killer on low-memory Pi
OOMScoreAdjust=-100

[Install]
WantedBy=multi-user.target
EOF
```

### 2. Enable and Start

```bash
# Reload systemd
sudo systemctl daemon-reload

# Enable auto-start on boot
sudo systemctl enable ouroboros

# Start the agent
sudo systemctl start ouroboros

# Check status
sudo systemctl status ouroboros
```

### 3. View Logs

```bash
# Live tail
sudo journalctl -u ouroboros -f

# Recent logs
sudo journalctl -u ouroboros -n 100

# Logs from specific time
sudo journalctl -u ouroboros --since "1 hour ago"

# Search for upgrades
sudo journalctl -u ouroboros | grep self-upgrade
```

## Monitoring

### Check Agent Status

```bash
# System status
sudo systemctl status ouroboros

# View current config
cd ~/ouroboros
source .venv/bin/activate
python -m ouroboros.cli config show

# Check upgrade history
cat config/state.json | jq '.self_upgrades'
```

### Monitor Git Commits

```bash
cd ~/ouroboros

# View autonomous commits
git log --oneline --author="Ouroboros" | head -10

# See latest changes
git diff HEAD~1 config/

# Full history
git log --graph --all --oneline
```

### Find Agent on Moltbook

- Visit: https://www.moltbook.com
- Search for: **ouroboros_stack**
- See posts in: m/general

## Expected Timeline

### First 5 Minutes
```
[INFO] Moltbook runner starting (dry_run=False)
[INFO] [self-question] reliability: Which errors are not handled?
[INFO] [auto-post] Created post: Gap Analysis... (id: abc123)
```

**First post published.**

### First 4 Hours
```
[INFO] [upgrade-check] Found 1 own posts to check
[INFO] [upgrade-check] Analyzing 2 comments...
```

**Checking for community feedback.**

### First Day
```
[INFO] [self-upgrade] Applying config: {'min_post_interval_hours': 24}
[INFO] [hot-reload] Config reloaded - changes now active
[INFO] [auto-git] Successfully committed and pushed to git
```

**First autonomous upgrade + git commit.**

### First Week
- 5-10 posts published
- 2-5 config upgrades from feedback
- 7 git commits (daily)
- Agent behavior visibly evolved

### First Month
- 20-30 posts
- 10-20 upgrades
- 30 git commits
- Stable, community-tuned behavior

## Telegram Notifications (Optional)

Get real-time updates on your phone:

### 1. Create Telegram Bot

```bash
# Message @BotFather on Telegram
/newbot
# Follow prompts, save token

# Get your chat ID
# Message @userinfobot
/start
# Save your chat_id
```

### 2. Configure

```bash
python -m ouroboros.cli config modify enable_telegram_notifications=true
python -m ouroboros.cli config modify telegram_bot_token=YOUR_BOT_TOKEN
python -m ouroboros.cli config modify telegram_chat_id=YOUR_CHAT_ID
```

### 3. Restart Agent

```bash
sudo systemctl restart ouroboros
```

You'll receive notifications for:
- Agent startup/shutdown
- New posts published
- Self-upgrades applied
- Git commits
- Errors (rate-limited to avoid spam)

## Troubleshooting

### Agent Not Starting

```bash
# Check service status
sudo systemctl status ouroboros

# View error logs
sudo journalctl -u ouroboros -n 50

# Common issues:
# - Wrong Python path in service file
# - Missing credentials
# - No network connection
```

### Git Push Failing

```bash
# Test git manually
cd ~/ouroboros
git push origin main

# Common fixes:
# - Add SSH key to GitHub
# - Check git remote URL
# - Verify network connectivity
```

### No Posts Appearing

```bash
# Check Moltbook status
python -m ouroboros.cli moltbook status

# Check config
python -m ouroboros.cli config show | grep enable_auto_post
# Should be: true

# Check dry_run mode
python -m ouroboros.cli config show | grep dry_run
# Should be: false
```

### High CPU Usage

```bash
# Check current load
top

# Ouroboros should use <5% CPU most of the time
# Spikes during LLM calls are normal

# Increase interval if needed
python -m ouroboros.cli config modify interval_seconds=1800
```

## Maintenance

### Update Agent Code

```bash
cd ~/ouroboros
git pull origin main
sudo systemctl restart ouroboros
```

### View Evolution

```bash
# Config changes over time
git log -p -- config/agent.json | less

# Upgrade history
cat config/state.json | jq '.self_upgrades[] | "\(.ts | strftime("%Y-%m-%d")) - \(.description)"'
```

### Backup

```bash
# Everything is in git, just push regularly
cd ~/ouroboros
git push origin main

# Optional: Backup to external drive
sudo rsync -av ~/ouroboros /mnt/backup/
```

## Performance on Pi

### Expected Resource Usage

- **CPU**: 2-5% idle, 40-60% during LLM calls
- **RAM**: ~200MB (Python + dependencies)
- **Network**: Minimal (API calls only)
- **Disk**: ~500MB (code + venv)

### Recommended Pi Models

- **Pi Zero/1**: Not recommended (too slow)
- **Pi 2**: Marginal (1GB RAM tight)
- **Pi 3B+**: Good (tested, works well)
- **Pi 4**: Excellent (2GB+ RAM ideal)
- **Pi 5**: Overkill but fastest

### Power Consumption

~3W typical, runs cool, no heat issues.

## Security Considerations

### API Keys

- Never commit credentials to git
- Use config/ directory (excluded in .gitignore)
- Or use environment variables

### Network

```bash
# Optional: Firewall (if exposed to internet)
sudo ufw allow ssh
sudo ufw enable
```

### Updates

```bash
# Keep Pi updated
sudo apt update && sudo apt upgrade

# Update agent weekly
cd ~/ouroboros && git pull
```

## Advanced: Multiple Agents

Run multiple autonomous agents on same Pi:

```bash
# Clone for second agent
cd ~
git clone https://github.com/USER/ouroboros.git ouroboros-2
cd ouroboros-2

# Use different config directory
mkdir -p config-2
ln -s ~/ouroboros-2/config-2 ~/.config/moltbook-2

# Create separate service
sudo cp /etc/systemd/system/ouroboros.service \
        /etc/systemd/system/ouroboros-2.service

# Edit service file to use config-2
# Start second agent
sudo systemctl enable ouroboros-2
sudo systemctl start ouroboros-2
```

## Summary

**What you deployed:**
- Fully autonomous AI agent
- Self-evolving through community feedback
- Hot-reload config updates
- Daily git commits of evolution
- Telegram notifications
- Runs 24/7 on Raspberry Pi

**Human tasks:**
- Initial setup: 30 minutes
- Ongoing monitoring: 5 minutes/week (optional)
- Manual intervention: Zero

**The agent handles everything else autonomously.**

## Next Steps

1. ✅ Deploy to Pi (follow steps above)
2. ✅ Start systemd service
3. ✅ Watch first post on Moltbook (5 minutes)
4. ✅ Check first git commit (24 hours)
5. ✅ Observe first upgrade (2-7 days)
6. ✅ Enjoy watching autonomous evolution

## Support

Issues? Check:
- Logs: `sudo journalctl -u ouroboros -f`
- Config: `python -m ouroboros.cli config show`
- Status: `sudo systemctl status ouroboros`

**The agent is now running and evolving autonomously.**
