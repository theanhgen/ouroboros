from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .model_defaults import DEFAULT_OPENAI_MODEL


@dataclass(frozen=True)
class SafetyConfig:
    """Configuration for the safety/policy layer of the self-improvement engine.

    Notes on require_human_approval:
        This flag means "require a human reviewer to approve the PR before merge"
        and only makes sense when CODEOWNERS or branch protection rules are configured
        on the repository. In the default autonomous setup there are no such rules,
        so the field defaults to False to match the actual enable_auto_merge=True
        behavior. Set it to True only when you have branch protection in place.
    """

    pr_only: bool = True
    allow_network: bool = True
    allow_write_default_branch: bool = False
    # See class docstring -- False matches the default enable_auto_merge=True behavior.
    require_human_approval: bool = False
    allow_self_modification: bool = True
    enable_auto_merge: bool = True  # Auto-merge PRs when checks pass
    max_retry_on_failure: int = 1  # Retry with root cause analysis on test regression

    # Sandbox configuration
    sandbox_enabled: bool = False
    sandbox_image: str = "python:3.11-slim"

    # Multi-model review
    reviewer_model: str = DEFAULT_OPENAI_MODEL

    # Backend selection for the self-improvement engine.
    #
    # "openai" (default) uses the OpenAI/Anthropic API client. "claude" or
    # "codex" route to the local CLI agent of that name (see backends.py).
    # generator_backend in agent mode lets the CLI edit the working tree
    # directly; reviewer_backend routes the peer-review step. generator_model
    # is an optional model override passed to the CLI (e.g. a claude model id).
    # Per-role backend routing. Each is "openai" (default), "claude", "codex",
    # or "agy". The self-improvement pipeline is: identify -> plan -> generate
    # -> review. generator/reviewer run as full file-editing agents; identify
    # and plan run as text/JSON completions.
    identify_backend: str = "openai"
    plan_backend: str = "openai"
    generator_backend: str = "openai"
    reviewer_backend: str = "openai"
    generator_model: Optional[str] = None

    # Reviewer-only OpenAI-compatible backend routing.
    #
    # If reviewer_base_url is set, the review step will use an OpenAI-compatible
    # client pointed at reviewer_base_url (with reviewer_api_key if provided).
    # This allows using Ollama Cloud (or any compatible gateway) for review
    # while keeping generation on the default OpenAI backend.
    reviewer_base_url: Optional[str] = None
    reviewer_api_key: Optional[str] = None

    # Self-improvement limits
    max_improvements_per_day: int = 3
    max_changed_files_per_pr: int = 3
    max_lines_changed_per_pr: int = 200

    # Path restrictions for the improvement engine
    allowed_modification_paths: Tuple[str, ...] = (
        "src/ouroboros/",
        "tests/",
        "docs/wiki/",
    )
    # The single source of truth for the immutable-file list. Both the
    # enforcement gate (improvement._is_path_allowed) and the version metrics
    # reports from (policies.validate_modification_scope) read this one tuple,
    # so they cannot disagree about what is immutable (#112).
    forbidden_modification_paths: Tuple[str, ...] = (
        "config.py",
        "improvement.py",
        "git_ops.py",
        "evaluation.py",
        "policies.py",
    )

    # Modules generated code may not import, checked by
    # policies.validate_import_policy. Matching covers submodules, so "socket"
    # also rejects "import socket.foo" and "from socket import socket".
    #
    # This is a lint-level guard, not a capability boundary. subprocess, os and
    # urllib stay importable because the codebase depends on them, so code that
    # passes this check can still spawn processes and reach the network. It
    # catches an improvement that casually reaches for pickle or ctypes; it does
    # not contain one that is trying to get out. Enforce real limits in a
    # sandbox.
    #
    # The default entries are unused anywhere in src/ or tests/, so it rejects
    # nothing that exists today -- a test asserts that against the real tree.
    forbidden_import_modules: Tuple[str, ...] = (
        "ctypes",
        "marshal",
        "pickle",
        "pty",
        "shelve",
        "socket",
    )


def reviewer_safety_kwargs(cfg: Any) -> Dict[str, Any]:
    """Map a runner config's reviewer settings onto SafetyConfig kwargs.

    Shared by the main loop and the scheduled runner so the review step is
    configured the same way in both.

    An unset reviewer_model leaves SafetyConfig's own default in place; it
    deliberately does *not* fall back to the generation model. Tying review to
    improvement_model is what the main loop used to do, and it makes a
    "second opinion" a second opinion from the same model. The scheduled runner
    never did it, so this also stops the two paths disagreeing.

    reviewer_base_url and reviewer_api_key are only included when set, so the
    SafetyConfig defaults (None) still mean "use the normal OpenAI client".
    """
    kwargs: Dict[str, Any] = {
        "reviewer_backend": getattr(cfg, "reviewer_backend", "openai"),
    }
    if getattr(cfg, "reviewer_model", ""):
        kwargs["reviewer_model"] = cfg.reviewer_model
    if getattr(cfg, "reviewer_base_url", ""):
        kwargs["reviewer_base_url"] = cfg.reviewer_base_url
    if getattr(cfg, "reviewer_api_key", None):
        kwargs["reviewer_api_key"] = cfg.reviewer_api_key
    return kwargs
