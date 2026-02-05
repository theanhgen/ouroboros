# Moltbook Agent Runner (Raspberry Pi)

## What this does
A minimal autonomous runner that checks Moltbook, tracks seen posts, and optionally comments on posts that match allowlisted keywords. It is PR-only safe by default and will not post or comment unless configured.

## Requirements
- Python 3.10+
- Moltbook API key saved in `~/.config/moltbook/credentials.json`

## Configure
Create `~/.config/moltbook/agent.json`:

```json
{
  "interval_seconds": 1800,
  "enable_auto_post": false,
  "enable_auto_comment": false,
  "keyword_allowlist": ["debug", "error", "stack trace"],
  "default_submolt": "general",
  "dry_run": true
}
```

Set `dry_run` to `false` only when you're confident.

## Run locally
```bash
cd /path/to/ouroboros
python -m ouroboros.cli moltbook-run
```

## Run as a service (systemd)
Create `/etc/systemd/system/ouroboros-moltbook.service`:

```ini
[Unit]
Description=Ouroboros Moltbook Runner
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/ouroboros
ExecStart=/usr/bin/python3 -m ouroboros.cli moltbook-run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ouroboros-moltbook.service
sudo systemctl status ouroboros-moltbook.service
```
