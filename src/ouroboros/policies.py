from dataclasses import dataclass


@dataclass(frozen=True)
class Evidence:
    source: str
    location: str
    note: str


class PolicyError(RuntimeError):
    pass


def require_pr_only(is_pr_only: bool) -> None:
    if not is_pr_only:
        raise PolicyError("PR-only policy violated")
