"""One description of what a RunnerConfig field may hold.

Two callers need this and had been diverging: the operator CLI, which accepted
any key and almost any value, and the comment-upgrade path, which decided what
a stranger may change with a deny-list -- so every field added since was
suggestible until someone remembered to list it.

Field names and types come from the dataclass. Only the bounds are written
down here, and only where a value is nonsensical rather than merely unusual.
"""

from dataclasses import fields as dataclass_fields
from typing import Any, Dict, List, NamedTuple, Optional, Tuple


class Bounds(NamedTuple):
    minimum: Optional[int] = None
    maximum: Optional[int] = None


# Ranges for numeric fields. A missing entry means "any positive integer",
# which is the floor applied to every int field: the loop sleeps on most of
# these, and zero or negative turns a typo into a busy loop.
_BOUNDS: Dict[str, Bounds] = {
    # Ceilings are sanity limits, not policy: a year of seconds, a day of
    # hours. They exist so a fat-fingered value is refused rather than
    # silently parking the agent for a decade.
    "interval_seconds": Bounds(1, 31_536_000),
    "min_comment_interval_seconds": Bounds(1, 31_536_000),
    "telegram_error_min_interval_seconds": Bounds(1, 31_536_000),
    "self_improvement_retry_minutes": Bounds(1, 525_600),
    "comment_check_interval_hours": Bounds(1, 8_760),
    "community_improvement_interval_hours": Bounds(1, 8_760),
    "community_wait_hours": Bounds(1, 8_760),
    "engagement_check_interval_hours": Bounds(1, 8_760),
    "git_push_interval_hours": Bounds(1, 8_760),
    "github_improvement_interval_hours": Bounds(1, 8_760),
    "improvement_interval_hours": Bounds(1, 8_760),
    "issue_scouting_interval_hours": Bounds(1, 8_760),
    "min_post_interval_hours": Bounds(1, 8_760),
    "self_improve_interval_hours": Bounds(1, 8_760),
    "self_question_hours": Bounds(1, 8_760),
    "wiki_update_interval_hours": Bounds(1, 8_760),
    # A count of zero is meaningful here -- it means "do not comment" -- so
    # these floor at 0 rather than 1.
    "max_comments_per_cycle": Bounds(0, 100),
    "community_min_comments_for_early": Bounds(0, 1_000),
    # An hour of the day.
    "oddities_digest_hour": Bounds(0, 23),
}

# Fields a public comment may suggest changing. An allowlist, not a deny-list:
# a field added later is operator-only until someone deliberately opens it,
# rather than suggestible until someone remembers to close it.
#
# Everything here is a rate or a threshold. Nothing that redirects traffic,
# names a credential, chooses a model, or switches a capability on.
COMMENT_SUGGESTIBLE_FIELDS = frozenset({
    "interval_seconds",
    "min_comment_interval_seconds",
    "max_comments_per_cycle",
    "min_post_interval_hours",
    "self_question_hours",
    "comment_check_interval_hours",
    "improvement_interval_hours",
    "community_wait_hours",
    "community_min_comments_for_early",
    "telegram_error_min_interval_seconds",
})

# Suggestions from a comment may only make the agent less active. An interval
# can grow, a count can shrink. "Post less often" is reasonable advice from a
# stranger; "poll ten times faster" is the thing the guard exists to prevent,
# and this makes a hostile suggestion self-limiting rather than relying on the
# bounds above to be tight enough.
_LOWER_IS_MORE_ACTIVE = frozenset({
    name for name in COMMENT_SUGGESTIBLE_FIELDS
    if name.endswith(("_seconds", "_hours", "_minutes"))
})


# Accepted by `config modify` but not RunnerConfig fields: self_modify routes
# them to credentials.json. ollama_api_key is the alias kept for the Ollama
# Cloud reviewer setup.
_CREDENTIAL_ALIASES: Dict[str, Any] = {"ollama_api_key": str}


def field_types() -> Dict[str, Any]:
    """Map every settable key to its declared type."""
    from .moltbook import RunnerConfig

    types: Dict[str, Any] = {f.name: f.type for f in dataclass_fields(RunnerConfig)}
    types.update(_CREDENTIAL_ALIASES)
    return types


def _is_int_field(declared: Any) -> bool:
    return declared in ("int", int)


def _is_bool_field(declared: Any) -> bool:
    return declared in ("bool", bool)


def validate(key: str, value: Any) -> Optional[str]:
    """Return an error message if key/value is not a valid config setting.

    Unknown keys are rejected. A silently ignored typo is the worst outcome:
    the command reports success and the setting never takes effect.
    """
    types = field_types()
    if key not in types:
        suggestion = _closest(key, types)
        hint = f"; did you mean {suggestion}?" if suggestion else ""
        return f"unknown setting {key!r}{hint}"

    declared = types[key]

    if _is_bool_field(declared):
        if not isinstance(value, bool):
            return f"{key} must be true or false"
        return None

    if _is_int_field(declared):
        # bool is an int subclass, and `enable_x=true` for a numeric field is
        # a mistake worth naming rather than storing as 1.
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{key} must be a whole number, got {value!r}"
        bounds = _BOUNDS.get(key, Bounds(1, None))
        if bounds.minimum is not None and value < bounds.minimum:
            return f"{key} must be at least {bounds.minimum}, got {value}"
        if bounds.maximum is not None and value > bounds.maximum:
            return f"{key} must be at most {bounds.maximum}, got {value}"
        return None

    # Strings and optional lists. Empty is meaningful for the string fields --
    # reviewer_base_url, reviewer_model and generator_model use it to mean
    # "unset", and clearing a compromised endpoint has to stay possible.
    if isinstance(value, (str, list)) or value is None:
        return None
    return f"{key} must be text, got {value!r}"


def validate_suggestion(key: str, value: Any, current: Any) -> Optional[str]:
    """Validate a change proposed by a public comment.

    Stricter than `validate`: restricted to the tuning allowlist, and only in
    the direction that makes the agent less active.
    """
    if key not in COMMENT_SUGGESTIBLE_FIELDS:
        return f"{key} may only be changed by an operator"

    error = validate(key, value)
    if error:
        return error

    if isinstance(current, int) and isinstance(value, int):
        if key in _LOWER_IS_MORE_ACTIVE and value < current:
            return f"{key} may only be increased by a suggestion ({current} -> {value})"
        if key not in _LOWER_IS_MORE_ACTIVE and value > current:
            return f"{key} may only be reduced by a suggestion ({current} -> {value})"

    return None


def _closest(key: str, known: Dict[str, Any]) -> Optional[str]:
    """Best near-match for an unknown key, for the error message."""
    import difflib

    matches = difflib.get_close_matches(key, list(known), n=1, cutoff=0.7)
    return matches[0] if matches else None


def parse_value(key: str, raw: str) -> Tuple[Any, Optional[str]]:
    """Coerce a command-line string to the type the field declares.

    Typed by the field rather than guessed from the text, so `improvement_
    interval_hours=true` is an error instead of being stored as a boolean, and
    a model name that happens to look numeric stays a string.
    """
    types = field_types()
    if key not in types:
        return None, validate(key, raw)

    declared = types[key]

    if _is_bool_field(declared):
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True, None
        if lowered in ("false", "0", "no", "off"):
            return False, None
        return None, f"{key} must be true or false, got {raw!r}"

    if _is_int_field(declared):
        try:
            return int(raw.strip()), None
        except ValueError:
            return None, f"{key} must be a whole number, got {raw!r}"

    if "List" in str(declared):
        items = [part.strip() for part in raw.split(",") if part.strip()]
        return (items or None), None

    return raw, None


def describe(key: str) -> str:
    """Human-readable description of what a field accepts."""
    types = field_types()
    if key not in types:
        return "unknown setting"
    declared = types[key]
    if _is_bool_field(declared):
        return "true or false"
    if _is_int_field(declared):
        bounds = _BOUNDS.get(key, Bounds(1, None))
        if bounds.maximum is None:
            return f"a whole number >= {bounds.minimum}"
        return f"a whole number from {bounds.minimum} to {bounds.maximum}"
    if "List" in str(declared):
        return "a comma-separated list"
    return "text"


def settable_keys() -> List[str]:
    return sorted(field_types())
