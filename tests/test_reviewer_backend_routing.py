from ouroboros import backends
import pytest


def test_safety_config_reviewer_fields_exist():
    # This test is intentionally light-weight: it guards the public config
    # surface used for reviewer-only routing.
    from ouroboros.config import SafetyConfig

    cfg = SafetyConfig()
    assert hasattr(cfg, "reviewer_model")
    assert hasattr(cfg, "reviewer_base_url")
    assert hasattr(cfg, "reviewer_api_key")


# -- the reviewer endpoint must actually be reachable ------------------------

def test_make_backend_client_returns_default_when_no_base_url():
    sentinel = object()
    assert backends.make_backend_client("openai", openai_client=sentinel) is sentinel


def test_make_backend_client_builds_a_client_for_a_compatible_endpoint(monkeypatch):
    """reviewer_base_url must produce a distinct client, not the generation one."""
    built = {}

    class FakeOpenAI:
        def __init__(self, api_key=None, base_url=None, max_retries=None):
            built["api_key"] = api_key
            built["base_url"] = base_url
            built["max_retries"] = max_retries

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    sentinel = object()

    client = backends.make_backend_client(
        "openai",
        openai_client=sentinel,
        model="qwen3-coder:480b-cloud",
        base_url="https://ollama.com/v1",
        api_key="secret",
    )

    assert client is not sentinel
    assert isinstance(client, FakeOpenAI)
    assert built == {
        "api_key": "secret",
        "base_url": "https://ollama.com/v1",
        "max_retries": 0,
    }


def test_make_backend_client_falls_back_when_endpoint_client_fails(monkeypatch):
    """An unusable gateway must not wedge the unattended loop."""
    def boom(*a, **kw):
        raise RuntimeError("nope")

    monkeypatch.setattr("openai.OpenAI", boom)
    sentinel = object()

    client = backends.make_backend_client(
        "openai", openai_client=sentinel, base_url="https://ollama.com/v1"
    )
    assert client is sentinel


def test_review_step_uses_the_configured_reviewer_endpoint(monkeypatch):
    """End-to-end: SafetyConfig -> make_backend_client -> review client."""
    from ouroboros.config import SafetyConfig

    captured = {}

    def fake_make_backend_client(backend, *, openai_client, model=None,
                                 base_url=None, api_key=None):
        captured.update(
            backend=backend, model=model, base_url=base_url, api_key=api_key
        )
        return "review-client"

    monkeypatch.setattr(backends, "make_backend_client", fake_make_backend_client)

    config = SafetyConfig(
        reviewer_model="qwen3-coder:480b-cloud",
        reviewer_base_url="https://ollama.com/v1",
        reviewer_api_key="secret",
    )
    client = backends.make_backend_client(
        getattr(config, "reviewer_backend", "openai"),
        openai_client="generation-client",
        model=config.reviewer_model,
        base_url=getattr(config, "reviewer_base_url", None),
        api_key=getattr(config, "reviewer_api_key", None),
    )

    assert client == "review-client"
    assert captured == {
        "backend": "openai",
        "model": "qwen3-coder:480b-cloud",
        "base_url": "https://ollama.com/v1",
        "api_key": "secret",
    }
