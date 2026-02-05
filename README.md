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

### Local Development
```bash
# Install dependencies
pip install openai>=1.0.0

# Configure credentials
mkdir -p ~/.config/moltbook
# Add credentials.json and openai.json (see RASPBERRY_PI_DEPLOYMENT.md)

# View current configuration
python -m ouroboros config show

# Run agent
python -m ouroboros moltbook run
```

### Raspberry Pi Deployment

**Complete autonomous setup - see [RASPBERRY_PI_DEPLOYMENT.md](RASPBERRY_PI_DEPLOYMENT.md)**

Quick version:
```bash
# 1. Clone and setup
git clone https://github.com/YOUR_USERNAME/ouroboros.git
cd ouroboros
python3 -m venv .venv && source .venv/bin/activate
pip install openai

# 2. Configure (add your keys)
mkdir -p config
# Add credentials.json, openai.json to config/

# 3. Deploy as systemd service
sudo cp systemd/ouroboros.service /etc/systemd/system/
sudo systemctl enable ouroboros
sudo systemctl start ouroboros

# 4. Watch it evolve
sudo journalctl -u ouroboros -f
```

Within 5 minutes, your agent posts its first insight to Moltbook.
Within 24 hours, it commits its first evolution to git.
Within a week, it has upgraded itself multiple times based on community feedback.

**All without human intervention.**

## Documentation

- **[RASPBERRY_PI_DEPLOYMENT.md](RASPBERRY_PI_DEPLOYMENT.md)** - Complete Pi setup guide (start here)
- **[COMMENT_BASED_UPGRADES.md](COMMENT_BASED_UPGRADES.md)** - How agent learns from feedback
- **[AUTO_GIT_PUSH.md](AUTO_GIT_PUSH.md)** - Daily autonomous commits
- **[HOT_RELOAD.md](HOT_RELOAD.md)** - Config updates without restart
- **[WHAT_HAPPENS_NOW.md](WHAT_HAPPENS_NOW.md)** - Timeline of autonomous operation
- **[FIRST_RUN.md](FIRST_RUN.md)** - What to expect in first 24 hours
- **[AUTONOMOUS_MODE.md](AUTONOMOUS_MODE.md)** - Technical implementation details

## Repository Layout
- `src/ouroboros/` - Core autonomous agent code
- `config/` - Agent configuration and state (tracked in git)
- `tests/` - Tests
- `docs/` - Design documents
