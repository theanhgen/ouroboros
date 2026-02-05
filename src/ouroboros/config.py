from dataclasses import dataclass


@dataclass(frozen=True)
class SafetyConfig:
    pr_only: bool = True
    allow_network: bool = False
    allow_write_default_branch: bool = False
    require_human_approval: bool = True
