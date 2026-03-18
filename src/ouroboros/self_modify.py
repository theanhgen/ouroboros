import json
import os
from typing import Any, Dict


class SelfModificationError(RuntimeError):
    pass


def can_self_modify() -> bool:
    """Check if self-modification is allowed by current config."""
    from .config import SafetyConfig
    config = SafetyConfig()
    return config.allow_self_modification


def modify_config(updates: Dict[str, Any], config_type: str = "safety") -> None:
    """
    Modify agent configuration without human approval.

    Args:
        updates: Dictionary of config keys to update
        config_type: Either "safety" or "runner"

    Raises:
        SelfModificationError: If self-modification is not allowed
    """
    if not can_self_modify():
        raise SelfModificationError("Self-modification is disabled in SafetyConfig")

    if config_type == "safety":
        # SafetyConfig is code-based, would require code modification
        raise SelfModificationError(
            "Safety config modification requires code changes. "
            "Use modify_runner_config for runtime configuration."
        )
    elif config_type == "runner":
        modify_runner_config(updates)
    else:
        raise ValueError(f"Unknown config_type: {config_type}")


def modify_runner_config(updates: Dict[str, Any]) -> None:
    """Modify the runner configuration file autonomously."""
    from .moltbook import load_runner_config

    updates = dict(updates)
    cfg_path = os.path.expanduser("~/.config/moltbook/agent.json")
    cred_path = os.path.expanduser("~/.config/moltbook/credentials.json")
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)

    # Load existing or create new
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}

    # Keep Telegram secrets out of tracked runtime config.
    secret_keys = ("telegram_bot_token", "telegram_chat_id")
    secret_updates = {
        key: updates.pop(key)
        for key in secret_keys
        if key in updates
    }
    if secret_updates:
        if os.path.exists(cred_path):
            with open(cred_path, "r", encoding="utf-8") as f:
                cred_data = json.load(f)
        else:
            cred_data = {}
        cred_data.update(secret_updates)
        cred_tmp_path = cred_path + ".tmp"
        with open(cred_tmp_path, "w", encoding="utf-8") as f:
            json.dump(cred_data, f, indent=2, sort_keys=True)
        os.replace(cred_tmp_path, cred_path)

    for key in secret_keys:
        data.pop(key, None)

    # Apply updates
    data.update(updates)

    # Write back
    tmp_path = cfg_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp_path, cfg_path)


def get_current_config() -> Dict[str, Any]:
    """Get current configuration state."""
    from .config import SafetyConfig
    from .moltbook import load_runner_config

    safety = SafetyConfig()
    runner = load_runner_config()

    return {
        "safety": {
            "pr_only": safety.pr_only,
            "allow_network": safety.allow_network,
            "allow_write_default_branch": safety.allow_write_default_branch,
            "require_human_approval": safety.require_human_approval,
            "allow_self_modification": safety.allow_self_modification,
        },
        "runner": {
            "interval_seconds": runner.interval_seconds,
            "enable_auto_comment": runner.enable_auto_comment,
            "dry_run": runner.dry_run,
            "enable_self_modification": runner.enable_self_modification,
            "self_question_hours": runner.self_question_hours,
            "max_comments_per_cycle": runner.max_comments_per_cycle,
            "min_comment_interval_seconds": runner.min_comment_interval_seconds,
            "enable_comment_based_upgrades": runner.enable_comment_based_upgrades,
            "comment_check_interval_hours": runner.comment_check_interval_hours,
            "auto_apply_config_suggestions": runner.auto_apply_config_suggestions,
            "self_improve_interval_hours": runner.self_improve_interval_hours,
            "self_improve_model": runner.self_improve_model,
            "enable_self_improvement": runner.enable_self_improvement,
            "enable_self_improvement_in_loop": runner.enable_self_improvement_in_loop,
            "improvement_interval_hours": runner.improvement_interval_hours,
            "self_improvement_retry_minutes": runner.self_improvement_retry_minutes,
            "improvement_model": runner.improvement_model,
            "enable_auto_issue_creation": runner.enable_auto_issue_creation,
            "enable_auto_git_push": runner.enable_auto_git_push,
            "git_push_interval_hours": runner.git_push_interval_hours,
            "enable_telegram_notifications": runner.enable_telegram_notifications,
            "telegram_bot_token": "***" if runner.telegram_bot_token else None,
            "telegram_chat_id": "***" if runner.telegram_chat_id else None,
            "telegram_error_min_interval_seconds": runner.telegram_error_min_interval_seconds,
        }
    }
