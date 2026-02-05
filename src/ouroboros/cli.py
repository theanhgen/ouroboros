import argparse
import json
import logging
from .config import SafetyConfig
from .policies import require_pr_only
from .moltbook import run_loop, get_status, get_feed, load_credentials
from .self_modify import get_current_config, modify_runner_config, can_self_modify


def cmd_plan(_args: argparse.Namespace) -> int:
    config = SafetyConfig()
    mode = "AUTONOMOUS" if not config.require_human_approval else "SUPERVISED"
    writes = "direct writes enabled" if config.allow_write_default_branch else "PR-only"
    self_mod = "self-modification enabled" if config.allow_self_modification else "config locked"
    print(f"Ouroboros plan: {mode}, {writes}, {self_mod}")
    return 0


def cmd_propose(_args: argparse.Namespace) -> int:
    config = SafetyConfig()
    if config.pr_only:
        require_pr_only(config.pr_only)
    print("Proposal stub: retrieval + agent swarm + evidence scoring")
    return 0


def cmd_apply(_args: argparse.Namespace) -> int:
    config = SafetyConfig()
    if config.pr_only:
        require_pr_only(config.pr_only)
        if config.require_human_approval:
            print("Apply blocked: human approval required")
            return 2
        print("Apply stub: would open PR")
    else:
        if config.allow_write_default_branch:
            print("Apply stub: would write directly to default branch (autonomous mode)")
        else:
            print("Apply blocked: direct writes disabled")
            return 2
    return 0


def cmd_moltbook_run(_args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return run_loop()


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


def cmd_config_show(_args: argparse.Namespace) -> int:
    config = get_current_config()
    print(json.dumps(config, indent=2))
    return 0


def cmd_config_modify(args: argparse.Namespace) -> int:
    if not can_self_modify():
        print("Self-modification is disabled. Set allow_self_modification=True in SafetyConfig.")
        return 2

    updates = {}
    for kv in args.updates:
        if "=" not in kv:
            print(f"Invalid update format: {kv}. Use key=value")
            return 1
        key, value = kv.split("=", 1)
        # Parse value
        if value.lower() == "true":
            updates[key] = True
        elif value.lower() == "false":
            updates[key] = False
        elif value.isdigit():
            updates[key] = int(value)
        else:
            updates[key] = value

    modify_runner_config(updates)
    print(f"Updated runner config: {updates}")
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

    p_cfg = sub.add_parser("config", help="Manage agent configuration")
    cfg_sub = p_cfg.add_subparsers(dest="cfg_command", required=True)

    p_show = cfg_sub.add_parser("show", help="Show current configuration")
    p_show.set_defaults(func=cmd_config_show)

    p_modify = cfg_sub.add_parser("modify", help="Modify configuration (autonomous mode)")
    p_modify.add_argument("updates", nargs="+", help="key=value pairs to update")
    p_modify.set_defaults(func=cmd_config_modify)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
