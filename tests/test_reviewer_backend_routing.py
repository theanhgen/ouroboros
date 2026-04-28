import pytest


def test_safety_config_reviewer_fields_exist():
    # This test is intentionally light-weight: it guards the public config
    # surface used for reviewer-only routing.
    from ouroboros.config import SafetyConfig

    cfg = SafetyConfig()
    assert hasattr(cfg, "reviewer_model")
    assert hasattr(cfg, "reviewer_base_url")
    assert hasattr(cfg, "reviewer_api_key")
