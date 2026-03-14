from ouroboros.config import SafetyConfig

def test_safety_config_defaults():
    config = SafetyConfig()
    assert config.pr_only is True
    assert config.allow_self_modification is True
    assert "src/ouroboros/" in config.allowed_modification_paths
    assert "config.py" in config.forbidden_modification_paths

def test_safety_config_override():
    config = SafetyConfig(pr_only=False, max_improvements_per_day=10)
    assert config.pr_only is False
    assert config.max_improvements_per_day == 10
