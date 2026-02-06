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


# -- Improve subcommands --

def cmd_improve_run(args: argparse.Namespace) -> int:
    """Run one improvement cycle."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    from . import llm
    from .improvement import run_improvement_cycle
    from .config import SafetyConfig

    openai_key = llm.load_openai_key()
    client = llm.make_client(openai_key)
    config = SafetyConfig()
    state = {}

    result = run_improvement_cycle(
        client, state, config,
        model=getattr(args, "model", "gpt-4o"),
        dry_run=getattr(args, "dry_run", False),
    )

    if result is None:
        print("No improvements identified or rate-limited.")
        return 0

    print(f"Improvement result: [{result.status}] {result.task.description}")
    if result.pr_url:
        print(f"PR: {result.pr_url}")
    return 0 if result.status in ("success", "skipped") else 1


def cmd_improve_status(_args: argparse.Namespace) -> int:
    """Show pending PRs and recent improvement history."""
    from .evaluation import load_history, check_pr_outcomes
    from .codebase import get_repo_root
    from . import git_ops

    repo = get_repo_root()
    history = check_pr_outcomes(repo)

    has_open = git_ops.has_open_improvement_prs(repo)
    print(f"Open improvement PRs: {'yes' if has_open else 'no'}")

    pending = [r for r in history if r.outcome == "pending"]
    if pending:
        print(f"\nPending PRs ({len(pending)}):")
        for r in pending:
            print(f"  - [{r.task_type}] {r.description}")
            if r.pr_url:
                print(f"    PR: {r.pr_url}")

    recent = history[-5:]
    if recent:
        print(f"\nRecent improvements ({len(recent)}):")
        for r in recent:
            print(f"  - [{r.outcome}] {r.task_type}: {r.description}")

    return 0


def cmd_improve_history(_args: argparse.Namespace) -> int:
    """Show full improvement history."""
    from .evaluation import load_history

    history = load_history()
    if not history:
        print("No improvement history.")
        return 0

    for r in history:
        delta = ""
        if r.test_delta:
            before = r.test_delta.get("before", {})
            after = r.test_delta.get("after", {})
            delta = (
                f" (tests: {before.get('passed', 0)}p/{before.get('failed', 0)}f"
                f" -> {after.get('passed', 0)}p/{after.get('failed', 0)}f)"
            )
        print(f"[{r.outcome}] {r.task_type}: {r.description}{delta}")
        if r.pr_url:
            print(f"  PR: {r.pr_url}")

    return 0


def cmd_improve_community(args: argparse.Namespace) -> int:
    """Run one community improvement step (or full dry-run cycle)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    from . import llm
    from .community_improvement import step_community_improvement, clear_community_improvement
    from .config import SafetyConfig
    from .moltbook import load_credentials, load_runner_config, load_state, save_state, RunnerConfig

    openai_key = llm.load_openai_key()
    client = llm.make_client(openai_key)
    safety = SafetyConfig()

    cfg = load_runner_config()
    # Force community improvement on and apply CLI flags
    cfg = RunnerConfig(
        **{
            **{f.name: getattr(cfg, f.name) for f in cfg.__dataclass_fields__.values()},
            "enable_community_improvement": True,
            "dry_run": getattr(args, "dry_run", False),
            "improvement_model": getattr(args, "model", "gpt-4o"),
        }
    )

    creds = load_credentials()
    state = load_state()

    # Clear interval gate for manual trigger
    state["last_community_improvement_start"] = None

    result = step_community_improvement(client, state, creds, cfg, safety)
    if result:
        print(f"Community improvement step: {result}")
    else:
        print("No action taken.")

    ci = state.get("community_improvement")
    if ci:
        print(f"Status: {ci.get('status')}")
        print(f"Task: [{ci.get('task_type')}] {ci.get('description')}")
        if ci.get("status") in ("completed", "failed"):
            clear_community_improvement(state)

    if not getattr(args, "dry_run", False):
        save_state(state)

    return 0


def cmd_improve_identify(args: argparse.Namespace) -> int:
    """Dry-run: identify improvements without acting."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    from . import llm
    from .improvement import run_improvement_cycle
    from .config import SafetyConfig

    openai_key = llm.load_openai_key()
    client = llm.make_client(openai_key)
    config = SafetyConfig()
    state = {}

    result = run_improvement_cycle(
        client, state, config,
        model=getattr(args, "model", "gpt-4o"),
        dry_run=True,
    )

    if result is None:
        print("No improvements identified.")
    else:
        print(f"Identified: [{result.task.task_type}] {result.task.description}")
        print(f"Target files: {result.task.target_files}")
        print(f"Evidence: {result.task.evidence}")

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

    # -- improve subcommand --
    p_improve = sub.add_parser("improve", help="Self-improvement tools")
    imp_sub = p_improve.add_subparsers(dest="imp_command", required=True)

    p_imp_run = imp_sub.add_parser("run", help="Run one improvement cycle")
    p_imp_run.add_argument("--model", default="gpt-4o", help="LLM model to use")
    p_imp_run.add_argument("--dry-run", action="store_true", help="Identify only, don't act")
    p_imp_run.set_defaults(func=cmd_improve_run)

    p_imp_status = imp_sub.add_parser("status", help="Show pending PRs and recent history")
    p_imp_status.set_defaults(func=cmd_improve_status)

    p_imp_history = imp_sub.add_parser("history", help="Show past improvements")
    p_imp_history.set_defaults(func=cmd_improve_history)

    p_imp_identify = imp_sub.add_parser("identify", help="Dry-run: identify but don't act")
    p_imp_identify.add_argument("--model", default="gpt-4o", help="LLM model to use")
    p_imp_identify.set_defaults(func=cmd_improve_identify)

    p_imp_community = imp_sub.add_parser("community", help="Run community-assisted improvement step")
    p_imp_community.add_argument("--model", default="gpt-4o", help="LLM model to use")
    p_imp_community.add_argument("--dry-run", action="store_true", help="Identify and generate post, don't publish")
    p_imp_community.set_defaults(func=cmd_improve_community)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
