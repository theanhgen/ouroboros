import json
import os
from typing import Any, Dict, Optional

from .storage import save_json_file


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


def filter_untrusted_config_updates(
    updates: Dict[str, Any], current: Optional[Dict[str, Any]] = None
) -> "tuple[Dict[str, Any], list]":
    """Split updates into (applicable, rejected) for a public suggestion.

    An allowlist with bounds, not a deny-list. Under a deny-list every field
    added to RunnerConfig was suggestible until someone remembered to close
    it; now a new field is operator-only until someone opens it. See
    config_schema.COMMENT_SUGGESTIBLE_FIELDS.

    Rejected entries come back as "key: reason" so the loop can log why.
    """
    from . import config_schema

    if current is None:
        from .moltbook import load_runner_config

        try:
            current = vars(load_runner_config())
        except Exception as exc:
            # Fail closed. Without the current values the direction rule
            # silently does nothing, so a decrease that should be refused
            # would be applied.
            return {}, [f"*: current configuration unreadable ({exc})"]

    safe: Dict[str, Any] = {}
    rejected = []
    for key, value in updates.items():
        value, error = config_schema.coerce_suggestion(key, value)
        if error is None:
            error = config_schema.validate_suggestion(key, value, current.get(key))
        if error:
            rejected.append(f"{key}: {error}")
        else:
            safe[key] = value
    return safe, sorted(rejected)


def modify_runner_config(updates: Dict[str, Any]) -> None:
    """Modify the runner configuration file autonomously.

    Applies whatever it is given. Callers relaying an untrusted suggestion must
    run it through filter_untrusted_config_updates first.
    """
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

    # Keep secrets out of tracked runtime config.
    secret_keys = (
        "telegram_bot_token",
        "telegram_chat_id",
        "reviewer_api_key",
        "ollama_api_key",
    )
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
        # ollama_api_key is an accepted alias for reviewer_api_key. Store one
        # canonical entry and drop the alias, otherwise setting one while the
        # other is present reports success while the loader keeps using the
        # stale value -- key rotation would silently not take effect.
        if "ollama_api_key" in secret_updates:
            secret_updates["reviewer_api_key"] = secret_updates.pop("ollama_api_key")
        if "reviewer_api_key" in secret_updates:
            cred_data.pop("ollama_api_key", None)
        cred_data.update(secret_updates)
        save_json_file(cred_path, cred_data, sort_keys=True, indent=2)

    for key in secret_keys:
        data.pop(key, None)

    # Apply updates
    data.update(updates)

    # Write back
    save_json_file(cfg_path, data, sort_keys=True, indent=2)


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
            "enable_issue_scouting": runner.enable_issue_scouting,
            "issue_scouting_interval_hours": runner.issue_scouting_interval_hours,
            "issue_scouting_model": runner.issue_scouting_model,
            "enable_telegram_notifications": runner.enable_telegram_notifications,
            "telegram_bot_token": "***" if runner.telegram_bot_token else None,
            "telegram_chat_id": "***" if runner.telegram_chat_id else None,
            "telegram_error_min_interval_seconds": runner.telegram_error_min_interval_seconds,
        }
    }
