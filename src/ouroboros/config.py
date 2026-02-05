from dataclasses import dataclass


@dataclass(frozen=True)
class SafetyConfig:
    pr_only: bool = False
    allow_network: bool = True
    allow_write_default_branch: bool = True
    require_human_approval: bool = False
    allow_self_modification: bool = True
