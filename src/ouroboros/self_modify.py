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

    cfg_path = os.path.expanduser("~/.config/moltbook/agent.json")
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)

    # Load existing or create new
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}

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
            "enable_auto_post": runner.enable_auto_post,
            "enable_auto_comment": runner.enable_auto_comment,
            "dry_run": runner.dry_run,
            "enable_self_modification": runner.enable_self_modification,
            "self_question_hours": runner.self_question_hours,
            "max_comments_per_cycle": runner.max_comments_per_cycle,
            "min_comment_interval_seconds": runner.min_comment_interval_seconds,
            "post_after_self_question": runner.post_after_self_question,
            "min_post_interval_hours": runner.min_post_interval_hours,
            "enable_comment_based_upgrades": runner.enable_comment_based_upgrades,
            "comment_check_interval_hours": runner.comment_check_interval_hours,
            "auto_apply_config_suggestions": runner.auto_apply_config_suggestions,
            "self_improve_interval_hours": runner.self_improve_interval_hours,
            "self_improve_model": runner.self_improve_model,
            "enable_auto_git_push": runner.enable_auto_git_push,
            "git_push_interval_hours": runner.git_push_interval_hours,
            "enable_telegram_notifications": runner.enable_telegram_notifications,
            "telegram_bot_token": "***" if runner.telegram_bot_token else None,
            "telegram_chat_id": runner.telegram_chat_id,
            "telegram_error_min_interval_seconds": runner.telegram_error_min_interval_seconds,
        }
    }
