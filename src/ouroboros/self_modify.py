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


# Config keys that only an operator may set.
#
# The comment-upgrade path feeds LLM-extracted key/value pairs from public
# comments straight into modify_runner_config, and both
# enable_comment_based_upgrades and auto_apply_config_suggestions default to
# True. Anything that redirects model traffic, names a credential, or widens
# the agent's own permissions therefore has to be refused on that path: a
# suggested reviewer_base_url would send the reviewer credential and the full
# proposed diff to a host chosen by a commenter, whose reply then decides
# whether the change is approved.
OPERATOR_ONLY_CONFIG_KEYS = frozenset({
    # Endpoint redirection -- exfiltration of credentials and source.
    "reviewer_base_url",
    "local_model_base_url",
    # Credentials.
    "reviewer_api_key",
    "ollama_api_key",
    "telegram_bot_token",
    "telegram_chat_id",
    # Model and backend routing -- chooses who sees the code.
    "reviewer_model",
    "reviewer_backend",
    "identify_backend",
    "plan_backend",
    "generator_backend",
    "generator_model",
    "improvement_model",
    "issue_scouting_model",
    "self_improve_model",
    "local_model",
    # Permission widening, including over this mechanism itself.
    "auto_apply_config_suggestions",
    "dry_run",
    # Destination and behaviour selectors, as opposed to rate tuning: where the
    # agent publishes, and whether it publishes at all.
    "default_submolt",
    "post_after_self_question",
})

# Every "enable_*" flag is operator-only. They are the switches that turn on
# automation which writes outside this process -- pushing branches, opening
# PRs and issues, posting comments, editing the wiki -- so a commenter must not
# be able to flip one on. Matching by prefix rather than by name means a flag
# added later is operator-only on arrival instead of settable from a comment
# until someone remembers to list it.
OPERATOR_ONLY_CONFIG_PREFIXES = ("enable_",)


def is_operator_only_config_key(key: str) -> bool:
    """Return True if key may only be set by an operator, not suggested."""
    return key in OPERATOR_ONLY_CONFIG_KEYS or key.startswith(
        OPERATOR_ONLY_CONFIG_PREFIXES
    )


def filter_untrusted_config_updates(
    updates: Dict[str, Any],
) -> tuple[Dict[str, Any], list]:
    """Split updates into (applicable, rejected_keys) for an untrusted source.

    Used for config changes suggested by public comments. Operator-driven
    paths such as the CLI call modify_runner_config directly and are not
    filtered.
    """
    safe = {k: v for k, v in updates.items() if not is_operator_only_config_key(k)}
    rejected = sorted(k for k in updates if is_operator_only_config_key(k))
    return safe, rejected


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
            "enable_issue_scouting": runner.enable_issue_scouting,
            "issue_scouting_interval_hours": runner.issue_scouting_interval_hours,
            "issue_scouting_model": runner.issue_scouting_model,
            "enable_telegram_notifications": runner.enable_telegram_notifications,
            "telegram_bot_token": "***" if runner.telegram_bot_token else None,
            "telegram_chat_id": "***" if runner.telegram_chat_id else None,
            "telegram_error_min_interval_seconds": runner.telegram_error_min_interval_seconds,
        }
    }
