import pytest

from ouroboros.config import SafetyConfig, reviewer_safety_kwargs
from ouroboros.model_defaults import DEFAULT_OPENAI_MODEL


def test_reviewer_routing_fields_default_none():
    cfg = SafetyConfig()
    assert cfg.reviewer_base_url is None
    assert cfg.reviewer_api_key is None


def test_reviewer_routing_fields_independent():
    cfg = SafetyConfig(
        reviewer_base_url="https://ollama.example/v1",
        reviewer_api_key="secret",
        reviewer_model="llama-3.1-70b",
    )
    assert cfg.reviewer_base_url == "https://ollama.example/v1"
    assert cfg.reviewer_api_key == "secret"
    assert cfg.reviewer_model == "llama-3.1-70b"


# -- reviewer_safety_kwargs: runner config -> SafetyConfig -------------------

class _Cfg:
    """Minimal stand-in for RunnerConfig."""

    def __init__(self, **kw):
        self.improvement_model = "gpt-generation"
        self.reviewer_model = ""
        self.reviewer_base_url = ""
        self.reviewer_api_key = None
        self.reviewer_backend = "openai"
        self.__dict__.update(kw)


def test_reviewer_kwargs_do_not_inherit_the_generation_model():
    """Review must not silently become a second opinion from the same model."""
    kwargs = reviewer_safety_kwargs(_Cfg())
    assert "reviewer_model" not in kwargs
    assert SafetyConfig(**kwargs).reviewer_model == DEFAULT_OPENAI_MODEL
    assert "reviewer_base_url" not in kwargs
    assert "reviewer_api_key" not in kwargs


def test_reviewer_kwargs_override_the_generation_model():
    kwargs = reviewer_safety_kwargs(_Cfg(reviewer_model="qwen3-coder:480b-cloud"))
    assert kwargs["reviewer_model"] == "qwen3-coder:480b-cloud"


def test_reviewer_kwargs_pass_through_base_url_and_key():
    kwargs = reviewer_safety_kwargs(
        _Cfg(
            reviewer_model="qwen3-coder:480b-cloud",
            reviewer_base_url="https://ollama.com/v1",
            reviewer_api_key="secret",
            reviewer_backend="openai",
        )
    )
    assert kwargs["reviewer_base_url"] == "https://ollama.com/v1"
    assert kwargs["reviewer_api_key"] == "secret"


def test_reviewer_kwargs_build_a_valid_safety_config():
    cfg = _Cfg(
        reviewer_model="qwen3-coder:480b-cloud",
        reviewer_base_url="https://ollama.com/v1",
        reviewer_api_key="secret",
    )
    safety = SafetyConfig(**reviewer_safety_kwargs(cfg))

    assert safety.reviewer_model == "qwen3-coder:480b-cloud"
    assert safety.reviewer_base_url == "https://ollama.com/v1"
    assert safety.reviewer_api_key == "secret"
    # Generation is untouched.
    assert safety.generator_backend == "openai"
    assert safety.generator_model is None


def test_reviewer_kwargs_leave_unset_fields_at_safety_defaults():
    safety = SafetyConfig(**reviewer_safety_kwargs(_Cfg()))
    assert safety.reviewer_base_url is None
    assert safety.reviewer_api_key is None


def test_reviewer_kwargs_tolerate_a_config_missing_the_fields():
    """Older RunnerConfig objects must not break the call sites."""

    class Bare:
        improvement_model = "gpt-generation"

    kwargs = reviewer_safety_kwargs(Bare())
    assert "reviewer_model" not in kwargs
    assert kwargs["reviewer_backend"] == "openai"
