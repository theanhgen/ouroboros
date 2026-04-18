import fcntl
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .model_defaults import DEFAULT_OPENAI_MODEL

log = logging.getLogger(__name__)

API_BASE = "https://www.moltbook.com/api/v1"
WEB_BASE = "https://www.moltbook.com"

_shutdown_event = threading.Event()

MAX_SELF_QUESTION_LOG = 200
MAX_BACKOFF_SECONDS = 900  # 15 min cap
MAX_SEEN_POST_IDS = 200
MAX_COMMENT_HISTORY = 100
MAX_SELF_UPGRADES = 50
MAX_COMMUNITY_HISTORY = 20


def _handle_shutdown(signum: int, _frame: Any) -> None:
    sig_name = signal.Signals(signum).name
    log.info("Received %s -- shutting down gracefully", sig_name)
    _shutdown_event.set()


class MoltbookError(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    api_key: str
    agent_name: str


def _read_json_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_credentials(*, require_agent_name: bool = True) -> Credentials:
    api_key = os.environ.get("MOLTBOOK_API_KEY")
    agent_name = os.environ.get("MOLTBOOK_AGENT_NAME")
    cred_path = os.path.expanduser("~/.config/moltbook/credentials.json")

    if (not api_key or not agent_name) and os.path.exists(cred_path):
        data = _read_json_file(cred_path)
        api_key = api_key or data.get("api_key")
        agent_name = agent_name or data.get("agent_name")

    if not api_key:
        raise MoltbookError("Missing API key. Set MOLTBOOK_API_KEY or credentials.json")
    if require_agent_name and not agent_name:
        raise MoltbookError("Missing agent name. Set MOLTBOOK_AGENT_NAME or credentials.json")

    return Credentials(api_key=api_key, agent_name=agent_name or "")


def _request(method: str, path: str, api_key: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{API_BASE}{path}"
    data = None
    headers = {"Authorization": f"Bearer {api_key}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload)
    except Exception as exc:  # pragma: no cover - network errors
        raise MoltbookError(f"Request failed: {exc}") from exc


def get_status(api_key: str) -> Dict[str, Any]:
    return _request("GET", "/agents/status", api_key)


def get_feed(api_key: str, sort: str = "new", limit: int = 10) -> Dict[str, Any]:
    return _request("GET", f"/posts?sort={sort}&limit={limit}", api_key)


def get_posts(api_key: str, sort: str = "new", limit: int = 10) -> Dict[str, Any]:
    return _request("GET", f"/posts?sort={sort}&limit={limit}", api_key)


def create_post(api_key: str, submolt: str, title: str, content: Optional[str] = None, url: Optional[str] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"submolt": submolt, "title": title}
    if content:
        body["content"] = content
    if url:
        body["url"] = url
    return _request("POST", "/posts", api_key, body)


def create_comment(api_key: str, post_id: str, content: str, parent_id: Optional[str] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"content": content}
    if parent_id:
        body["parent_id"] = parent_id
    return _request("POST", f"/posts/{post_id}/comments", api_key, body)


def _post_url(post_id: Optional[str]) -> Optional[str]:
    if not post_id:
        return None
    return f"{WEB_BASE}/post/{post_id}"


def _comment_url(post_id: Optional[str], comment_id: Optional[str]) -> Optional[str]:
    if not post_id or not comment_id:
        return None
    return f"{WEB_BASE}/post/{post_id}#comment-{comment_id}"


def get_post_comments(api_key: str, post_id: str) -> Dict[str, Any]:
    """Fetch all comments for a specific post."""
    return _request("GET", f"/posts/{post_id}/comments", api_key)


def get_my_posts(api_key: str, agent_name: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Fetch posts authored by this agent."""
    feed = get_feed(api_key, sort="new", limit=50)
    posts = feed.get("posts", [])
    return [p for p in posts if p.get("author", {}).get("name") == agent_name][:limit]


@dataclass
class RunnerConfig:
    interval_seconds: int = 1800
    enable_auto_comment: bool = False  # Disabled: low-value LLM spam
    keyword_allowlist: Optional[List[str]] = None
    default_submolt: str = "general"
    dry_run: bool = False
    enable_telegram_notifications: bool = False
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_error_min_interval_seconds: int = 300
    # Deprecated alias for improvement_interval_hours.
    self_improve_interval_hours: int = 48
    self_improve_model: str = DEFAULT_OPENAI_MODEL
    self_question_hours: int = 8
    max_comments_per_cycle: int = 3
    min_comment_interval_seconds: int = 300
    enable_self_modification: bool = True
    enable_comment_based_upgrades: bool = True
    comment_check_interval_hours: int = 4
    auto_apply_config_suggestions: bool = True
    enable_auto_git_push: bool = False
    git_push_interval_hours: int = 24
    # Self-improvement settings
    enable_self_improvement: bool = False
    enable_self_improvement_in_loop: bool = True
    improvement_interval_hours: int = 48
    self_improvement_retry_minutes: int = 60
    improvement_model: str = DEFAULT_OPENAI_MODEL
    improvement_types: Optional[List[str]] = None  # default: ["fix_test", "add_test", "fix_bug", "refactor", "improve_docs", "add_feature"]
    enable_auto_issue_creation: bool = True
    enable_auto_merge: bool = False  # Auto-merge PRs when checks pass
    # Feed intelligence pipeline
    enable_comment_mining: bool = False
    enable_engagement_tracking: bool = False
    engagement_check_interval_hours: int = 6
    enable_knowledge_base: bool = False
    # Daily oddities digest
    enable_oddities_digest: bool = False
    oddities_digest_hour: int = 20  # send at ~8 PM
    # Community-assisted improvement
    enable_community_improvement: bool = False
    community_wait_hours: int = 48
    community_min_comments_for_early: int = 3
    community_improvement_interval_hours: int = 72
    # GitHub issue resolution
    enable_github_improvement: bool = False
    github_improvement_interval_hours: int = 12
    # GitHub issue scouting
    enable_issue_scouting: bool = False
    issue_scouting_interval_hours: int = 24
    issue_scouting_model: str = DEFAULT_OPENAI_MODEL
    # Wiki self-documentation
    enable_wiki: bool = False
    wiki_update_interval_hours: int = 24
    # Local model (Ollama) settings
    local_model: str = ""
    local_model_base_url: str = "http://localhost:11434/v1"
    use_local_for_cheap_tasks: bool = True


def load_runner_config() -> RunnerConfig:
    cfg_path = os.path.expanduser("~/.config/moltbook/agent.json")
    data: Dict[str, Any] = {}
    if os.path.exists(cfg_path):
        data = _read_json_file(cfg_path)

    # Keep sensitive Telegram values out of tracked config.
    cred_path = os.path.expanduser("~/.config/moltbook/credentials.json")
    cred_data: Dict[str, Any] = {}
    if os.path.exists(cred_path):
        cred_data = _read_json_file(cred_path)

    telegram_bot_token = (
        os.environ.get("TELEGRAM_BOT_TOKEN")
        or cred_data.get("telegram_bot_token")
    )
    telegram_chat_id = (
        os.environ.get("TELEGRAM_CHAT_ID")
        or cred_data.get("telegram_chat_id")
        or data.get("telegram_chat_id")
    )
    legacy_interval = int(data.get("self_improve_interval_hours", 48))
    improvement_interval = int(
        data.get(
            "improvement_interval_hours",
            legacy_interval if "self_improve_interval_hours" in data else 48,
        )
    )

    return RunnerConfig(
        interval_seconds=int(data.get("interval_seconds", 1800)),
        enable_auto_comment=bool(data.get("enable_auto_comment", False)),
        keyword_allowlist=data.get("keyword_allowlist"),
        default_submolt=data.get("default_submolt", "general"),
        dry_run=bool(data.get("dry_run", False)),
        enable_telegram_notifications=bool(data.get("enable_telegram_notifications", False)),
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        telegram_error_min_interval_seconds=int(
            data.get("telegram_error_min_interval_seconds", 300)
        ),
        self_improve_interval_hours=improvement_interval,
        self_improve_model=str(data.get("self_improve_model", DEFAULT_OPENAI_MODEL)),
        self_question_hours=int(data.get("self_question_hours", 8)),
        max_comments_per_cycle=int(data.get("max_comments_per_cycle", 3)),
        min_comment_interval_seconds=int(data.get("min_comment_interval_seconds", 300)),
        enable_self_modification=bool(data.get("enable_self_modification", True)),
        enable_comment_based_upgrades=bool(data.get("enable_comment_based_upgrades", True)),
        comment_check_interval_hours=int(data.get("comment_check_interval_hours", 4)),
        auto_apply_config_suggestions=bool(data.get("auto_apply_config_suggestions", True)),
        enable_auto_git_push=bool(data.get("enable_auto_git_push", False)),
        git_push_interval_hours=int(data.get("git_push_interval_hours", 24)),
        enable_comment_mining=bool(data.get("enable_comment_mining", False)),
        enable_engagement_tracking=bool(data.get("enable_engagement_tracking", False)),
        engagement_check_interval_hours=int(data.get("engagement_check_interval_hours", 6)),
        enable_knowledge_base=bool(data.get("enable_knowledge_base", False)),
        enable_oddities_digest=bool(data.get("enable_oddities_digest", False)),
        oddities_digest_hour=int(data.get("oddities_digest_hour", 20)),
        enable_self_improvement=bool(data.get("enable_self_improvement", False)),
        enable_self_improvement_in_loop=bool(data.get("enable_self_improvement_in_loop", True)),
        improvement_interval_hours=improvement_interval,
        self_improvement_retry_minutes=int(data.get("self_improvement_retry_minutes", 60)),
        improvement_model=str(data.get("improvement_model", DEFAULT_OPENAI_MODEL)),
        improvement_types=data.get("improvement_types"),
        enable_auto_issue_creation=bool(data.get("enable_auto_issue_creation", True)),
        enable_auto_merge=bool(data.get("enable_auto_merge", False)),
        enable_community_improvement=bool(data.get("enable_community_improvement", False)),
        community_wait_hours=int(data.get("community_wait_hours", 48)),
        community_min_comments_for_early=int(data.get("community_min_comments_for_early", 3)),
        community_improvement_interval_hours=int(data.get("community_improvement_interval_hours", 72)),
        enable_github_improvement=bool(data.get("enable_github_improvement", False)),
        github_improvement_interval_hours=int(data.get("github_improvement_interval_hours", 12)),
        enable_issue_scouting=bool(data.get("enable_issue_scouting", False)),
        issue_scouting_interval_hours=int(data.get("issue_scouting_interval_hours", 24)),
        issue_scouting_model=str(data.get("issue_scouting_model", data.get("improvement_model", DEFAULT_OPENAI_MODEL))),
        enable_wiki=bool(data.get("enable_wiki", False)),
        wiki_update_interval_hours=int(data.get("wiki_update_interval_hours", 24)),
        local_model=str(data.get("local_model", "")),
        local_model_base_url=str(data.get("local_model_base_url", "http://localhost:11434/v1")),
        use_local_for_cheap_tasks=bool(data.get("use_local_for_cheap_tasks", True)),
    )


def _state_path() -> str:
    return os.path.expanduser("~/.config/moltbook/state.json")


def load_state() -> Dict[str, Any]:
    path = _state_path()
    if not os.path.exists(path):
        return {
            "last_check": None,
            "last_post": None,
            "last_self_question": None,
            "last_self_improve": None,
            "last_comment_time": None,
            "self_question_index": 0,
            "self_question_log": [],
            "comment_history": [],
            "seen_post_ids": [],
            "community_improvement": None,
            "community_improvement_history": [],
            "last_community_improvement_start": None,
            "last_issue_scouting_attempt": None,
        }
    return _read_json_file(path)


def save_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _trim_state(state)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(state, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    os.replace(tmp_path, path)


def _send_telegram_message(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception:
        log.exception("Failed to send Telegram message")


def _notify(
    cfg: RunnerConfig,
    state: Dict[str, Any],
    message: str,
    *,
    is_error: bool = False,
) -> None:
    if not cfg.enable_telegram_notifications:
        return
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        return
    if is_error:
        now = int(time.time())
        last_ts = int(state.get("last_telegram_error_ts", 0) or 0)
        if now - last_ts < cfg.telegram_error_min_interval_seconds:
            return
        state["last_telegram_error_ts"] = now
    _send_telegram_message(cfg.telegram_bot_token, cfg.telegram_chat_id, message)


def _shorten(text: str, limit: int = 400) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _trim_self_question_log(state: Dict[str, Any]) -> None:
    log_list = state.get("self_question_log", [])
    if len(log_list) > MAX_SELF_QUESTION_LOG:
        state["self_question_log"] = log_list[-MAX_SELF_QUESTION_LOG:]


def _trim_comment_history(state: Dict[str, Any], limit: int = 80) -> None:
    history = state.get("comment_history", [])
    if len(history) > limit:
        state["comment_history"] = history[-limit:]


def _trim_feed_suggestions(state: Dict[str, Any], limit: int = 30) -> None:
    suggestions = state.get("feed_improvement_suggestions", [])
    if len(suggestions) > limit:
        state["feed_improvement_suggestions"] = suggestions[-limit:]


def _trim_engagement_scores(state: Dict[str, Any], limit: int = 50) -> None:
    scores = state.get("engagement_scores", [])
    if len(scores) > limit:
        state["engagement_scores"] = scores[-limit:]


def _trim_state(state: Dict[str, Any]) -> None:
    """Apply all size caps to state dict before saving."""
    seen = state.get("seen_post_ids", [])
    if len(seen) > MAX_SEEN_POST_IDS:
        state["seen_post_ids"] = seen[-MAX_SEEN_POST_IDS:]

    _trim_comment_history(state, limit=MAX_COMMENT_HISTORY)
    _trim_self_question_log(state)

    upgrades = state.get("self_upgrades", [])
    if len(upgrades) > MAX_SELF_UPGRADES:
        state["self_upgrades"] = upgrades[-MAX_SELF_UPGRADES:]

    community_hist = state.get("community_improvement_history", [])
    if len(community_hist) > MAX_COMMUNITY_HISTORY:
        state["community_improvement_history"] = community_hist[-MAX_COMMUNITY_HISTORY:]


def _check_engagement(
    cfg: RunnerConfig,
    creds: Credentials,
    state: Dict[str, Any],
    openai_client: Any,
) -> None:
    """Check engagement on recent comments and extract topic signals."""
    from . import llm

    comment_history = state.get("comment_history", [])
    now = int(time.time())
    seven_days_ago = now - 7 * 86400

    # Get comments from last 7 days that have a comment_id
    recent = [
        c for c in comment_history
        if c.get("comment_id") and c.get("ts", 0) >= seven_days_ago
    ]

    # Deduplicate by post_id
    seen_posts = {}
    for c in recent:
        pid = c.get("post_id")
        if pid and pid not in seen_posts:
            seen_posts[pid] = c

    existing_scores = {
        s.get("post_id"): s
        for s in state.get("engagement_scores", [])
    }

    for post_id, comment_data in seen_posts.items():
        # Skip if already checked recently (< 24h ago)
        existing = existing_scores.get(post_id)
        if existing and (now - existing.get("checked_at", 0)) < 86400:
            continue

        try:
            post_comments = get_post_comments(creds.api_key, post_id)
            all_comments = post_comments.get("comments", [])

            # Find bot's comment votes and count replies after it
            bot_comment_id = comment_data.get("comment_id")
            bot_ts = comment_data.get("ts", 0)
            bot_upvotes = 0
            bot_downvotes = 0
            replies = []

            for c in all_comments:
                if c.get("id") == bot_comment_id:
                    bot_upvotes = c.get("upvotes", 0)
                    bot_downvotes = c.get("downvotes", 0)
                    continue
                # Comments posted after the bot's comment are considered replies
                c_created = c.get("created_at", c.get("ts", 0))
                if isinstance(c_created, str):
                    continue  # skip unparseable timestamps
                if c_created and int(c_created) >= bot_ts:
                    replies.append(c.get("content", ""))

            has_engagement = replies or bot_upvotes > 0 or bot_downvotes > 0
            if has_engagement:
                topic_signal = None
                if replies:
                    topic_signal = llm.extract_topic_signal(
                        openai_client,
                        comment_data.get("title", ""),
                        comment_data.get("comment", ""),
                        replies,
                    )
                entry = {
                    "post_id": post_id,
                    "post_title": comment_data.get("title", ""),
                    "bot_comment": comment_data.get("comment", ""),
                    "reply_count": len(replies),
                    "upvotes": bot_upvotes,
                    "downvotes": bot_downvotes,
                    "topic_signal": topic_signal,
                    "checked_at": now,
                }
                # Update existing or append
                if existing:
                    existing.update(entry)
                else:
                    state.setdefault("engagement_scores", []).append(entry)

        except Exception:
            log.exception("Engagement check failed for post %s", post_id)


_GIT_POLL_INTERVAL = 60  # Check for upstream changes every 60s during sleep


def _interruptible_sleep(seconds: int, *, check_git: bool = False) -> None:
    """Sleep that returns early when shutdown is requested.

    If check_git is True, polls for upstream changes every 60s and exits
    the process for systemd restart if source files changed.
    """
    log.debug("Sleeping %ds (next cycle)", seconds)
    if not check_git:
        _shutdown_event.wait(timeout=seconds)
        return

    remaining = seconds
    while remaining > 0 and not _shutdown_event.is_set():
        chunk = min(remaining, _GIT_POLL_INTERVAL)
        _shutdown_event.wait(timeout=chunk)
        remaining -= chunk
        if _shutdown_event.is_set():
            break
        # Check for upstream source changes
        try:
            from . import git_ops as _git_ops
            repo_root = Path(__file__).resolve().parents[2]
            if _git_ops.pull_latest(repo_root):
                log.info("Source files changed during sleep, exiting for systemd restart...")
                os._exit(0)
        except Exception:
            log.debug("Git poll during sleep failed, will retry next chunk")


def _auto_git_push(state: Dict[str, Any], dry_run: bool = False) -> bool:
    """Commit and push state/config to git. Returns True if successful."""
    import subprocess

    try:
        # Check if we're in a git repo
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            log.debug("Not in a git repository, skipping auto-push")
            return False

        repo_root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()

        # Collect stats for commit message
        history = sorted(state.get("community_improvement_history", []) + state.get("self_upgrades", []), key=lambda x: x.get('ts', 0))
        upgrade_count = len(history)
        question_count = len(state.get("self_question_log", []))
        post_count = 1 if state.get("last_post") else 0

        # Build commit message
        commit_msg = f"""Autonomous update - {time.strftime('%Y-%m-%d %H:%M:%S')}

Stats:
- Self-upgrades applied: {upgrade_count}
- Self-questions answered: {question_count}
- Last post: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(state.get('last_post', 0))) if state.get('last_post') else 'never'}
- Last upgrade: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(history[-1].get('ts', 0))) if history else 'none'}

🤖 Generated autonomously by Ouroboros
"""

        if dry_run:
            log.info("[dry-run] Would git commit and push with message:\n%s", commit_msg)
            return True

        # Add config and state files
        config_file = os.path.expanduser("~/.config/moltbook/agent.json")
        state_file = os.path.expanduser("~/.config/moltbook/state.json")

        def _is_under_repo(path: str, repo_root: str) -> bool:
            try:
                return os.path.commonpath([os.path.abspath(path), repo_root]) == repo_root
            except ValueError:
                return False

        # Check if these files exist and are inside the repo
        files_to_add = []
        if os.path.exists(config_file) and _is_under_repo(config_file, repo_root):
            files_to_add.append(config_file)
        else:
            log.debug("Config file not in repo, skipping: %s", config_file)

        if os.path.exists(state_file) and _is_under_repo(state_file, repo_root):
            files_to_add.append(state_file)
        else:
            log.debug("State file not in repo, skipping: %s", state_file)

        if not files_to_add:
            log.debug("No config/state files to commit")
            return False

        # Stage files
        subprocess.run(
            ["git", "add"] + files_to_add,
            cwd=repo_root,
            check=True,
            timeout=10,
        )

        # Check if there are changes to commit
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_root,
            timeout=5,
        )

        if result.returncode == 0:
            log.debug("No changes to commit")
            return True

        # Commit
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=repo_root,
            check=True,
            timeout=10,
        )

        # Pull before push to incorporate merged PRs
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=repo_root,
            check=False,
            timeout=30,
        )

        # Push
        subprocess.run(
            ["git", "push"],
            cwd=repo_root,
            check=True,
            timeout=30,
        )

        log.info("[auto-git] Successfully committed and pushed to git")
        return True

    except subprocess.TimeoutExpired:
        log.warning("Git operation timed out")
        return False
    except subprocess.CalledProcessError as e:
        log.warning("Git operation failed: %s", e)
        return False
    except Exception:
        log.exception("Unexpected error during git auto-push")
        return False


def run_loop() -> int:
    from . import llm
    from .self_question import DEFAULT_QUESTIONS, choose_question, record_question, get_questions_with_codebase

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    _shutdown_event.clear()

    cfg = load_runner_config()
    state = load_state()

    try:
        creds: Optional[Credentials] = load_credentials()
    except MoltbookError as exc:
        creds = None
        log.warning(
            "Moltbook credentials unavailable (%s). "
            "Feed polling, commenting, engagement tracking, comment-based upgrades, "
            "and community improvement are disabled.",
            exc,
        )

    # Fail fast if OpenAI key is missing
    openai_key = llm.load_openai_key()
    openai_client = llm.make_client(openai_key)
    import os
    log.info("Moltbook runner starting (dry_run=%s)", cfg.dry_run)
    _notify(cfg, state, f"Moltbook runner started (dry_run={cfg.dry_run}, PID={os.getpid()})")

    local_client = None
    if cfg.local_model:
        try:
            local_url = cfg.local_model_base_url
            # health check -- quick models list call
            urllib.request.urlopen(f"{local_url.rstrip('/v1').rstrip('/')}/api/tags", timeout=5).read()
            local_client = llm.make_local_client(local_url)
            log.info("Local Ollama client ready (%s at %s)", cfg.local_model, local_url)
        except Exception:
            log.warning("Ollama not reachable at %s -- local model disabled for this run", cfg.local_model_base_url)
            local_client = None

    def _pick_client(model: str) -> Any:
        """Return local_client when model is local and available, else openai_client."""
        if local_client and llm._is_local_model(model):
            return local_client
        return openai_client

    consecutive_errors = 0

    while not _shutdown_event.is_set():
        try:
            now = int(time.time())

            # -- Pull latest and restart if source changed --
            from . import git_ops as _git_ops
            repo_root = Path(__file__).resolve().parents[2]
            source_changed = _git_ops.pull_latest(repo_root)
            if source_changed:
                log.info("Source files changed after pull, exiting for systemd restart...")
                save_state(state)
                _notify(cfg, state, "Source files updated, restarting via systemd.")
                # Exit cleanly -- systemd Restart=always will relaunch with correct ExecStart args.
                # Using os._exit to avoid running atexit handlers that might interfere.
                os._exit(0)

            posts: List[Dict[str, Any]] = []
            new_posts: List[Dict[str, Any]] = []
            if creds is not None:
                status = get_status(creds.api_key)
                if status.get("status") != "claimed":
                    log.info("Not claimed yet. Sleeping %ds.", cfg.interval_seconds)
                    _interruptible_sleep(cfg.interval_seconds)
                    continue

                feed = get_feed(creds.api_key, sort="new", limit=10)
                posts = feed.get("posts") or feed.get("data") or []

                seen = set(state.get("seen_post_ids", []))
                new_posts = [p for p in posts if p.get("id") and p.get("id") not in seen]

                # -- Auto-comment with LLM and rate limiting --
                if cfg.enable_auto_comment:
                    comments_this_cycle = 0
                    last_comment_time = state.get("last_comment_time")

                    # Build codebase context so comments can reference real stats
                    _comment_codebase_ctx = ""
                    try:
                        from .codebase import get_codebase_summary, get_repo_root as _get_repo
                        from .metrics import get_summary as _get_metrics_summary
                        from .evaluation import load_history as _load_hist
                        from collections import Counter as _Counter

                        _rr = _get_repo()
                        _comment_codebase_ctx = get_codebase_summary(_rr)

                        # Add real metrics
                        _metrics = _get_metrics_summary(_rr)
                        if _metrics:
                            _comment_codebase_ctx += f"\n\nYour live metrics:\n{_metrics}"

                        # Add per-task-type stats from history
                        _hist = _load_hist(_rr)
                        if _hist:
                            _attempts = _Counter()
                            _reverts = _Counter()
                            _successes = _Counter()
                            for _r in _hist:
                                _attempts[_r.task_type] += 1
                                if _r.outcome in ("merged", "success"):
                                    _successes[_r.task_type] += 1
                                elif _r.outcome == "reverted":
                                    _reverts[_r.task_type] += 1
                            _lines = ["\nYour task-type stats (USE THESE REAL NUMBERS in comments):"]
                            for _tt, _total in _attempts.most_common():
                                _s = _successes.get(_tt, 0)
                                _rv = _reverts.get(_tt, 0)
                                _lines.append(
                                    f"- {_tt}: {_total} attempts, {_s} succeeded, "
                                    f"{_rv} reverted ({int(100*_rv/_total) if _total else 0}% revert rate)"
                                )
                            _comment_codebase_ctx += "\n".join(_lines)
                    except Exception:
                        log.debug("Could not load codebase context for comments")

                    for post in new_posts:
                        if _shutdown_event.is_set():
                            break
                        if comments_this_cycle >= cfg.max_comments_per_cycle:
                            log.debug("Reached max_comments_per_cycle (%d)", cfg.max_comments_per_cycle)
                            break

                        now_ts = int(time.time())
                        if last_comment_time is not None and (now_ts - int(last_comment_time)) < cfg.min_comment_interval_seconds:
                            log.debug("Comment interval not elapsed, skipping remaining posts")
                            break

                        # Filter by keywords if allowlist is configured
                        if cfg.keyword_allowlist:
                            text = f"{post.get('title', '')} {post.get('content', '')}".lower()
                            if not any(k.lower() in text for k in cfg.keyword_allowlist):
                                continue

                        _comment_client = local_client if local_client else openai_client
                        comment_text = llm.generate_comment(
                            _comment_client,
                            post.get("title", ""),
                            post.get("content", ""),
                            model=cfg.local_model if local_client else DEFAULT_OPENAI_MODEL,
                            codebase_context=_comment_codebase_ctx,
                        )
                        if comment_text is None:
                            log.warning("LLM failed to generate comment for post %s", post.get("id"))
                            continue

                        comment_result = None
                        if cfg.dry_run:
                            log.info("[dry-run] Would comment on %s: %s", post.get("id"), comment_text)
                        else:
                            comment_result = create_comment(creds.api_key, post.get("id"), comment_text)
                            log.info("Commented on post %s", post.get("id"))
                            post_url = _post_url(post.get("id"))
                            comment_url = _comment_url(post.get("id"), comment_result.get("id"))
                            _notify(
                                cfg,
                                state,
                                f"Commented: {_shorten(post.get('title', '') or '', 100)}"
                                + (f"\nPost: {post_url}" if post_url else "")
                                + (f"\nComment: {comment_url}" if comment_url else ""),
                            )

                        comment_entry = {
                            "post_id": post.get("id"),
                            "title": post.get("title", ""),
                            "content": post.get("content", ""),
                            "comment": comment_text,
                            "ts": int(time.time()),
                        }
                        if not cfg.dry_run and comment_result:
                            comment_entry["comment_id"] = comment_result.get("id")
                        state.setdefault("comment_history", []).append(comment_entry)

                        # -- Comment mining: extract codebase improvement insights --
                        if (
                            cfg.enable_comment_mining
                            and not cfg.dry_run
                            and comment_result
                        ):
                            try:
                                insight = llm.mine_insight_for_codebase(
                                    openai_client,
                                    post.get("title", ""),
                                    post.get("content", ""),
                                    comment_text,
                                )
                                if insight:
                                    state.setdefault("feed_improvement_suggestions", []).append(
                                        {
                                            "post_id": post.get("id"),
                                            "post_title": post.get("title", ""),
                                            "insight": insight,
                                            "ts": int(time.time()),
                                        }
                                    )
                                    log.info("[comment-mining] Insight: %s", insight[:100])
                            except Exception:
                                log.exception("Comment mining failed for post %s", post.get("id"))

                        comments_this_cycle += 1
                        state["last_comment_time"] = int(time.time())
                        last_comment_time = state["last_comment_time"]

                for post in new_posts:
                    pid = post.get("id")
                    if pid:
                        seen.add(pid)

                state["seen_post_ids"] = list(seen)[-500:]
                state["last_check"] = int(time.time())

                # -- Engagement tracking --
                if cfg.enable_engagement_tracking:
                    now_engage = int(time.time())
                    last_engage = state.get("last_engagement_check")
                    should_check_engage = (
                        last_engage is None
                        or (now_engage - int(last_engage)) >= cfg.engagement_check_interval_hours * 3600
                    )
                    if should_check_engage:
                        try:
                            _check_engagement(cfg, creds, state, openai_client)
                            state["last_engagement_check"] = now_engage
                            log.info("[engagement] Check complete")
                        except Exception:
                            log.exception("Engagement tracking failed")

                # -- Knowledge base population --
                if cfg.enable_knowledge_base and new_posts:
                    try:
                        from .knowledge_base import add_entries

                        kb_entries = []
                        commented_post_ids = {
                            c.get("post_id")
                            for c in state.get("comment_history", [])[-20:]
                        }

                        # Posts we commented on: use bot's comment as insight (no LLM call)
                        for c in state.get("comment_history", [])[-cfg.max_comments_per_cycle:]:
                            pid = c.get("post_id")
                            if pid in {p.get("id") for p in new_posts}:
                                kb_entries.append({
                                    "post_id": pid,
                                    "post_title": c.get("title", ""),
                                    "insight": c.get("comment", ""),
                                    "tags": [],
                                    "ts": int(time.time()),
                                    "source": "comment",
                                })

                        # Remaining posts: batch extract via LLM
                        overflow = [
                            p for p in new_posts
                            if p.get("id") not in commented_post_ids
                        ]
                        if overflow:
                            batch_insights = llm.extract_insights_batch(
                                openai_client, overflow[:5],
                            )
                            if batch_insights:
                                for item in batch_insights:
                                    idx = item.get("post_index", 0)
                                    if 0 <= idx < len(overflow):
                                        kb_entries.append({
                                            "post_id": overflow[idx].get("id"),
                                            "post_title": overflow[idx].get("title", ""),
                                            "insight": item.get("insight", ""),
                                            "tags": item.get("tags", []),
                                            "ts": int(time.time()),
                                            "source": "extraction",
                                        })

                        if kb_entries:
                            add_entries(kb_entries)
                            log.info("[knowledge-base] Added %d entries", len(kb_entries))
                    except Exception:
                        log.exception("Knowledge base population failed")

                # -- Daily oddities digest --
                if cfg.enable_oddities_digest:
                    import datetime
                    now_dt = datetime.datetime.now()
                    last_oddities = state.get("last_oddities_digest")
                    sent_today = (
                        last_oddities is not None
                        and (now - int(last_oddities)) < 86400
                    )
                    if (
                        not sent_today
                        and now_dt.hour >= cfg.oddities_digest_hour
                        and posts
                    ):
                        try:
                            _odds_client = local_client if local_client else openai_client
                            _odds_model = cfg.local_model if local_client else DEFAULT_OPENAI_MODEL
                            digest = llm.pick_oddities(_odds_client, posts, model=_odds_model)
                            if digest:
                                _notify(cfg, state, f"Daily Oddities Digest:\n\n{digest}")
                                state["last_oddities_digest"] = now
                                log.info("[oddities] Sent daily digest")
                        except Exception:
                            log.exception("Oddities digest failed")

            # -- Self-questioning with LLM answers --
            last_sq = state.get("last_self_question")

            if last_sq is None or now - int(last_sq) >= cfg.self_question_hours * 3600:
                questions = get_questions_with_codebase()
                question, idx = choose_question(state, questions)
                from .codebase import get_codebase_summary, get_repo_root
                sq_codebase = get_codebase_summary(get_repo_root())
                _sq_client = local_client if local_client else openai_client
                _sq_model = cfg.local_model if local_client else DEFAULT_OPENAI_MODEL
                answer = llm.answer_question(_sq_client, question.question, codebase_summary=sq_codebase, model=_sq_model)
                record_question(state, question, answer=answer)
                state["last_self_question"] = now
                state["self_question_index"] = idx + 1
                log.info("[self-question] %s: %s", question.area, question.question)
                if answer:
                    log.info("[self-answer] %s", answer)
                    _notify(
                        cfg,
                        state,
                        f"Q [{question.area}]: {_shorten(question.question, 120)}",
                    )

                    # Wire actionable self-question answers into improvement suggestions
                    if question.area in ("missing_tests", "test_failure", "safety", "reliability", "privacy", "refactoring", "edge_cases", "docs", "llm_optimization", "productivity"):
                        existing_titles = {
                            s.get("post_title")
                            for s in state.get("feed_improvement_suggestions", [])
                        }
                        if question.question not in existing_titles:
                            state.setdefault("feed_improvement_suggestions", []).append({
                                "post_id": f"sq-{now}",
                                "post_title": question.question,
                                "insight": answer,
                                "ts": now,
                            })
                            log.info("[self-question] Forwarded '%s' answer to improvement suggestions", question.area)

            # -- Comment-based self-upgrades --
            config_was_modified = False
            if creds is not None and cfg.enable_comment_based_upgrades and cfg.enable_self_modification:
                last_comment_check = state.get("last_comment_check")
                should_check_comments = (
                    last_comment_check is None or
                    (now - int(last_comment_check)) >= cfg.comment_check_interval_hours * 3600
                )

                if should_check_comments:
                    try:
                        my_posts = get_my_posts(creds.api_key, creds.agent_name, limit=5)
                        log.debug("[upgrade-check] Found %d own posts to check", len(my_posts))

                        for post in my_posts:
                            post_id = post.get("id")
                            if not post_id:
                                continue

                            # Check if we've already processed this post's comments
                            processed = state.get("processed_comment_posts", [])
                            if post_id in processed:
                                continue

                            comment_data = get_post_comments(creds.api_key, post_id)
                            comments = comment_data.get("comments", [])

                            if not comments:
                                continue

                            log.info(
                                "[upgrade-check] Analyzing %d comments on post: %s",
                                len(comments),
                                post.get("title", "")[:50],
                            )

                            analysis = llm.analyze_comments_for_upgrades(
                                openai_client,
                                post.get("title", ""),
                                post.get("content", ""),
                                comments,
                            )

                            if analysis and analysis.get("has_suggestions"):
                                suggestions = analysis.get("suggestions", [])
                                log.info(
                                    "[upgrade-check] Found %d actionable suggestions",
                                    len(suggestions),
                                )

                                for suggestion in suggestions:
                                    if suggestion.get("type") == "config_change" and cfg.auto_apply_config_suggestions:
                                        config_changes = suggestion.get("config_changes", {})
                                        if config_changes:
                                            from .self_modify import modify_runner_config

                                            if cfg.dry_run:
                                                log.info(
                                                    "[dry-run] Would apply config: %s (suggested by %s)",
                                                    config_changes,
                                                    suggestion.get("commenter", "unknown"),
                                                )
                                            else:
                                                log.info(
                                                    "[self-upgrade] Applying config: %s (suggested by %s: %s)",
                                                    config_changes,
                                                    suggestion.get("commenter", "unknown"),
                                                    suggestion.get("description", ""),
                                                )
                                                modify_runner_config(config_changes)
                                                config_was_modified = True
                                                _notify(
                                                    cfg,
                                                    state,
                                                    f"Config updated ({suggestion.get('commenter', 'unknown')}): "
                                                    f"{_shorten(suggestion.get('description', ''), 150)}",
                                                )

                                                # Track what was changed
                                                state.setdefault("self_upgrades", []).append(
                                                    {
                                                        "ts": now,
                                                        "post_id": post_id,
                                                        "commenter": suggestion.get("commenter"),
                                                        "description": suggestion.get("description"),
                                                        "changes": config_changes,
                                                    }
                                                )
                                    else:
                                        log.info(
                                            "[upgrade-check] Suggestion logged (type=%s): %s",
                                            suggestion.get("type"),
                                            suggestion.get("description", "")[:80],
                                        )

                            # Mark as processed
                            state.setdefault("processed_comment_posts", []).append(post_id)
                            state["processed_comment_posts"] = state["processed_comment_posts"][-50:]

                        state["last_comment_check"] = now

                    except Exception:
                        log.exception("Error during comment-based upgrade check")
                        _notify(
                            cfg,
                            state,
                            "Error during comment-based upgrade check",
                            is_error=True,
                        )

            # -- Hot-reload config if it was modified --
            if config_was_modified:
                log.info("[hot-reload] Configuration was modified, reloading...")
                cfg = load_runner_config()
                log.info("[hot-reload] Config reloaded - changes now active")
                # Rebuild local client if local_model config changed
                if cfg.local_model:
                    try:
                        local_client = llm.make_local_client(cfg.local_model_base_url)
                        log.info("[hot-reload] Local client refreshed for %s", cfg.local_model)
                    except Exception:
                        log.warning("[hot-reload] Failed to refresh local client")
                        local_client = None
                else:
                    local_client = None

            # -- Self-improvement cycle --
            if cfg.enable_self_improvement and cfg.enable_self_improvement_in_loop:
                last_improvement = state.get("last_improvement_attempt")
                should_improve = (
                    last_improvement is None or
                    (now - int(last_improvement)) >= cfg.improvement_interval_hours * 3600
                )

                if should_improve:
                    try:
                        from .improvement import run_improvement_cycle
                        from .evaluation import check_pr_outcomes
                        from .config import SafetyConfig

                        safety = SafetyConfig(
                            enable_auto_merge=cfg.enable_auto_merge,
                            reviewer_model=cfg.improvement_model,
                        )

                        # Telegram notification callback for improvement events
                        def _on_improve_event(event_type: str, message: str, data: dict) -> None:
                            # Notify on significant events
                            if event_type in (
                                "task_identified", "pr_created", "auto_merged",
                                "reverted", "baseline_broken", "retry_success",
                            ):
                                _notify(cfg, state, message)
                            elif event_type == "failed":
                                _notify(cfg, state, message, is_error=True)

                        # Update history with merged/closed PR outcomes
                        check_pr_outcomes(repo_root)

                        # Skip if open PRs exist
                        if not _git_ops.has_open_improvement_prs(repo_root):
                            log.info("[self-improve] Starting improvement cycle...")
                            imp_result = run_improvement_cycle(
                                _pick_client(cfg.improvement_model), state, safety,
                                model=cfg.improvement_model,
                                dry_run=cfg.dry_run,
                                on_event=_on_improve_event,
                            )
                            state["last_improvement_attempt"] = now

                            if imp_result:
                                log.info(
                                    "[self-improve] Result: [%s] %s",
                                    imp_result.status,
                                    imp_result.task.description,
                                )
                                # PR notification is now handled by on_event callback
                            else:
                                log.info("[self-improve] No improvements identified")
                        else:
                            log.debug("[self-improve] Skipping: open improvement PRs exist")
                            state["last_improvement_attempt"] = now

                    except Exception:
                        log.exception("Error during self-improvement cycle")
                        state["last_improvement_attempt"] = now
                        _notify(
                            cfg, state,
                            "Error during self-improvement cycle",
                            is_error=True,
                        )

            # -- GitHub issue resolution cycle --
            if cfg.enable_github_improvement:
                last_github = state.get("last_github_improvement_attempt")
                should_gh_improve = (
                    last_github is None or
                    (now - int(last_github)) >= cfg.github_improvement_interval_hours * 3600
                )

                if should_gh_improve:
                    try:
                        from .github_improvement import run_github_improvement_cycle
                        from .codebase import get_repo_root

                        repo_root = get_repo_root()
                        log.info("[github-improve] Checking for open issues...")
                        gh_results = run_github_improvement_cycle(
                            _pick_client(cfg.improvement_model), repo_root,
                            model=cfg.improvement_model,
                            dry_run=cfg.dry_run,
                            enable_auto_merge=cfg.enable_auto_merge,
                        )
                        state["last_github_improvement_attempt"] = now

                        for res in gh_results:
                            if res.status == "success" and res.pr_url:
                                log.info("[github-improve] Fixed issue #%d: %s", res.issue_id, res.description)
                                _notify(
                                    cfg, state,
                                    f"Fixed Issue #{res.issue_id}: {res.description[:100]}\n"
                                    f"{res.pr_url}",
                                )
                            elif res.status == "failed":
                                log.warning("[github-improve] Failed to fix issue #%d: %s", res.issue_id, res.error)
                    except Exception:
                        log.exception("[github-improve] Failed during GitHub improvement cycle")

            # -- GitHub issue scouting cycle --
            if cfg.enable_issue_scouting:
                last_issue_scouting = state.get("last_issue_scouting_attempt")
                should_scout = (
                    last_issue_scouting is None or
                    (now - int(last_issue_scouting)) >= cfg.issue_scouting_interval_hours * 3600
                )

                if should_scout:
                    try:
                        from .issue_scouting import run_issue_scouting_cycle

                        log.info("[issue-scout] Looking for new improvement opportunities...")
                        scout_result = run_issue_scouting_cycle(
                            _pick_client(cfg.issue_scouting_model),
                            repo_root,
                            model=cfg.issue_scouting_model,
                            dry_run=cfg.dry_run,
                        )
                        state["last_issue_scouting_attempt"] = now

                        if scout_result.status == "created" and scout_result.issue_url:
                            log.info("[issue-scout] Created issue: %s", scout_result.issue_url)
                            _notify(
                                cfg,
                                state,
                                f"Issue scout opened: {scout_result.task.description[:100]}\n"
                                f"{scout_result.issue_url}",
                            )
                        elif scout_result.status == "duplicate":
                            log.info("[issue-scout] Duplicate issue already open: %s", scout_result.issue_url)
                        elif scout_result.status == "idle":
                            log.info("[issue-scout] No improvement opportunities identified")
                        elif scout_result.status == "dry_run":
                            log.info("[issue-scout] %s", scout_result.message)
                        else:
                            log.warning("[issue-scout] %s", scout_result.message)
                            _notify(
                                cfg,
                                state,
                                scout_result.message,
                                is_error=True,
                            )
                    except Exception:
                        log.exception("[issue-scout] Failed during issue scouting cycle")
                        state["last_issue_scouting_attempt"] = now
                        _notify(
                            cfg,
                            state,
                            "Error during issue scouting cycle",
                            is_error=True,
                        )

            # -- Community-assisted improvement --
            if creds is not None and cfg.enable_community_improvement:
                try:
                    from .community_improvement import step_community_improvement, clear_community_improvement
                    from .config import SafetyConfig as _SafetyConfig

                    ci_safety = _SafetyConfig()
                    ci_result = step_community_improvement(
                        _pick_client(cfg.improvement_model), state, creds, cfg, ci_safety,
                    )
                    if ci_result:
                        log.info("[community] Step result: %s", ci_result)

                    # Clear completed/failed improvements
                    ci_state = state.get("community_improvement")
                    if ci_state and ci_state.get("status") in ("completed", "failed"):
                        clear_community_improvement(state)

                    save_state(state)
                except Exception:
                    log.exception("Error during community improvement step")
                    _notify(
                        cfg, state,
                        "Error during community improvement step",
                        is_error=True,
                    )

            # -- Wiki update (once per day) --
            if cfg.enable_wiki:
                last_wiki = state.get("last_wiki_update")
                should_wiki = (
                    last_wiki is None or
                    (now - int(last_wiki)) >= cfg.wiki_update_interval_hours * 3600
                )
                if should_wiki:
                    try:
                        from .wiki import update_wiki
                        from .codebase import get_repo_root as _get_wiki_repo
                        wiki_root = _get_wiki_repo()
                        updated_pages = update_wiki(wiki_root)
                        state["last_wiki_update"] = now
                        if updated_pages:
                            log.info("[wiki] Updated %d pages", len(updated_pages))
                    except Exception:
                        log.exception("[wiki] Failed to update wiki")

            # -- Memory hygiene (once per day) --
            last_mem_hygiene = state.get("last_memory_hygiene")
            should_hygiene = (
                last_mem_hygiene is None or
                (now - int(last_mem_hygiene)) >= 86400
            )
            if should_hygiene:
                try:
                    from .memory import IndexManager as _MemIndex
                    _mem = _MemIndex()
                    removed = _mem.run_hygiene()
                    state["last_memory_hygiene"] = now
                    if removed:
                        log.info("[memory] Hygiene: removed %d low-trust facts", removed)
                except Exception:
                    log.debug("[memory] Hygiene failed")

            # -- Auto git push (once per day) --
            if cfg.enable_auto_git_push:
                last_git_push = state.get("last_git_push")
                last_git_push_attempt = state.get("last_git_push_attempt")
                should_git_push = (
                    last_git_push_attempt is None or
                    (now - int(last_git_push_attempt)) >= cfg.git_push_interval_hours * 3600
                )

                if should_git_push:
                    state["last_git_push_attempt"] = now
                    log.info("[auto-git] Attempting to commit and push to git...")
                    success = _auto_git_push(state, dry_run=cfg.dry_run)
                    if success:
                        state["last_git_push"] = now
                        log.info("[auto-git] Next push in %d hours", cfg.git_push_interval_hours)
                    else:
                        _notify(cfg, state, "Auto-git push failed.", is_error=True)

            _trim_self_question_log(state)
            _trim_comment_history(state)
            _trim_feed_suggestions(state)
            _trim_engagement_scores(state)
            save_state(state)
            consecutive_errors = 0

        except Exception:
            consecutive_errors += 1
            backoff = min(cfg.interval_seconds * (2 ** (consecutive_errors - 1)), MAX_BACKOFF_SECONDS)
            log.exception("Error in run_loop cycle (%d consecutive). Backing off %ds.", consecutive_errors, backoff)
            _notify(
                cfg,
                state,
                f"Error in run_loop cycle ({consecutive_errors} consecutive). "
                f"Backing off {backoff}s.",
                is_error=True,
            )
            if not _shutdown_event.is_set():
                _interruptible_sleep(backoff)
            continue

        if not _shutdown_event.is_set():
            log.info("Sleeping %ds until next cycle", cfg.interval_seconds)
            _interruptible_sleep(cfg.interval_seconds, check_git=True)

    log.info("Moltbook runner stopped.")
    _notify(cfg, state, f"Moltbook runner stopped. (PID={os.getpid()})")
    save_state(state)
    return 0
