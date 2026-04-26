import pytest

from ouroboros.config import SafetyConfig


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
