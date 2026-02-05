import argparse
from .config import SafetyConfig
from .policies import require_pr_only
from .moltbook import run_loop, get_status, get_feed, load_credentials


def cmd_plan(_args: argparse.Namespace) -> int:
    config = SafetyConfig()
    require_pr_only(config.pr_only)
    print("Ouroboros plan: PR-only, read-only analysis, human approval required")
    return 0


def cmd_propose(_args: argparse.Namespace) -> int:
    config = SafetyConfig()
    require_pr_only(config.pr_only)
    print("Proposal stub: retrieval + agent swarm + evidence scoring")
    return 0


def cmd_apply(_args: argparse.Namespace) -> int:
    config = SafetyConfig()
    require_pr_only(config.pr_only)
    if config.require_human_approval:
        print("Apply blocked: human approval required")
        return 2
    print("Apply stub: would open PR")
    return 0


def cmd_moltbook_run(_args: argparse.Namespace) -> int:
    run_loop()
    return 0


def cmd_moltbook_status(_args: argparse.Namespace) -> int:
    creds = load_credentials()
    status = get_status(creds.api_key)
    print(status)
    return 0


def cmd_moltbook_feed(args: argparse.Namespace) -> int:
    creds = load_credentials()
    feed = get_feed(creds.api_key, sort=args.sort, limit=args.limit)
    print(feed)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ouroboros")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="Show current safety and workflow plan")
    p_plan.set_defaults(func=cmd_plan)

    p_prop = sub.add_parser("propose", help="Generate a proposal from a question")
    p_prop.set_defaults(func=cmd_propose)

    p_apply = sub.add_parser("apply", help="Apply proposal as a PR (gated)")
    p_apply.set_defaults(func=cmd_apply)

    p_mb = sub.add_parser("moltbook", help="Moltbook tools")
    mb_sub = p_mb.add_subparsers(dest="mb_command", required=True)

    p_run = mb_sub.add_parser("run", help="Run autonomous Moltbook loop")
    p_run.set_defaults(func=cmd_moltbook_run)

    p_status = mb_sub.add_parser("status", help="Check Moltbook claim status")
    p_status.set_defaults(func=cmd_moltbook_status)

    p_feed = mb_sub.add_parser("feed", help="Fetch Moltbook feed")
    p_feed.add_argument("--sort", default="new")
    p_feed.add_argument("--limit", type=int, default=10)
    p_feed.set_defaults(func=cmd_moltbook_feed)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
