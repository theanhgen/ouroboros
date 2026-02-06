import fcntl
import json
import logging
import os
import signal
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

API_BASE = "https://www.moltbook.com/api/v1"
WEB_BASE = "https://www.moltbook.com"

_shutdown_event = threading.Event()

MAX_SELF_QUESTION_LOG = 200
MAX_BACKOFF_SECONDS = 900  # 15 min cap


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


def load_credentials() -> Credentials:
    api_key = os.environ.get("MOLTBOOK_API_KEY")
    agent_name = os.environ.get("MOLTBOOK_AGENT_NAME")
    cred_path = os.path.expanduser("~/.config/moltbook/credentials.json")

    if (not api_key or not agent_name) and os.path.exists(cred_path):
        data = _read_json_file(cred_path)
        api_key = api_key or data.get("api_key")
        agent_name = agent_name or data.get("agent_name")

    if not api_key:
        raise MoltbookError("Missing API key. Set MOLTBOOK_API_KEY or credentials.json")
    if not agent_name:
        raise MoltbookError("Missing agent name. Set MOLTBOOK_AGENT_NAME or credentials.json")

    return Credentials(api_key=api_key, agent_name=agent_name)


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
    return _request("GET", f"/feed?sort={sort}&limit={limit}", api_key)


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
    enable_auto_post: bool = True
    enable_auto_comment: bool = True
    keyword_allowlist: Optional[List[str]] = None
    default_submolt: str = "general"
    dry_run: bool = False
    enable_telegram_notifications: bool = False
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_error_min_interval_seconds: int = 300
    self_improve_interval_hours: int = 24
    self_improve_model: str = "gpt-4o-mini"
    self_question_hours: int = 8
    max_comments_per_cycle: int = 3
    min_comment_interval_seconds: int = 300
    enable_self_modification: bool = True
    post_after_self_question: bool = True
    min_post_interval_hours: int = 12
    enable_comment_based_upgrades: bool = True
    comment_check_interval_hours: int = 4
    auto_apply_config_suggestions: bool = True
    enable_auto_git_push: bool = True
    git_push_interval_hours: int = 24
    # Self-improvement settings
    enable_self_improvement: bool = False
    improvement_interval_hours: int = 48
    improvement_model: str = "gpt-4o"
    improvement_types: Optional[List[str]] = None  # default: ["fix_test", "add_test", "fix_bug"]
    # Community-assisted improvement
    enable_community_improvement: bool = False
    community_wait_hours: int = 48
    community_min_comments_for_early: int = 3
    community_improvement_interval_hours: int = 72


def load_runner_config() -> RunnerConfig:
    cfg_path = os.path.expanduser("~/.config/moltbook/agent.json")
    if not os.path.exists(cfg_path):
        return RunnerConfig()
    data = _read_json_file(cfg_path)
    return RunnerConfig(
        interval_seconds=int(data.get("interval_seconds", 1800)),
        enable_auto_post=bool(data.get("enable_auto_post", True)),
        enable_auto_comment=bool(data.get("enable_auto_comment", True)),
        keyword_allowlist=data.get("keyword_allowlist"),
        default_submolt=data.get("default_submolt", "general"),
        dry_run=bool(data.get("dry_run", False)),
        enable_telegram_notifications=bool(data.get("enable_telegram_notifications", False)),
        telegram_bot_token=data.get("telegram_bot_token"),
        telegram_chat_id=data.get("telegram_chat_id"),
        telegram_error_min_interval_seconds=int(
            data.get("telegram_error_min_interval_seconds", 300)
        ),
        self_improve_interval_hours=int(data.get("self_improve_interval_hours", 24)),
        self_improve_model=str(data.get("self_improve_model", "gpt-4o-mini")),
        self_question_hours=int(data.get("self_question_hours", 8)),
        max_comments_per_cycle=int(data.get("max_comments_per_cycle", 3)),
        min_comment_interval_seconds=int(data.get("min_comment_interval_seconds", 300)),
        enable_self_modification=bool(data.get("enable_self_modification", True)),
        post_after_self_question=bool(data.get("post_after_self_question", True)),
        min_post_interval_hours=int(data.get("min_post_interval_hours", 12)),
        enable_comment_based_upgrades=bool(data.get("enable_comment_based_upgrades", True)),
        comment_check_interval_hours=int(data.get("comment_check_interval_hours", 4)),
        auto_apply_config_suggestions=bool(data.get("auto_apply_config_suggestions", True)),
        enable_auto_git_push=bool(data.get("enable_auto_git_push", True)),
        git_push_interval_hours=int(data.get("git_push_interval_hours", 24)),
        enable_self_improvement=bool(data.get("enable_self_improvement", False)),
        improvement_interval_hours=int(data.get("improvement_interval_hours", 48)),
        improvement_model=str(data.get("improvement_model", "gpt-4o")),
        improvement_types=data.get("improvement_types"),
        enable_community_improvement=bool(data.get("enable_community_improvement", False)),
        community_wait_hours=int(data.get("community_wait_hours", 48)),
        community_min_comments_for_early=int(data.get("community_min_comments_for_early", 3)),
        community_improvement_interval_hours=int(data.get("community_improvement_interval_hours", 72)),
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
        }
    return _read_json_file(path)


def save_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
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


def _interruptible_sleep(seconds: int) -> None:
    """Sleep that returns early when shutdown is requested."""
    log.debug("Sleeping %ds (next cycle)", seconds)
    _shutdown_event.wait(timeout=seconds)


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
        upgrade_count = len(state.get("self_upgrades", []))
        question_count = len(state.get("self_question_log", []))
        post_count = 1 if state.get("last_post") else 0

        # Build commit message
        commit_msg = f"""Autonomous update - {time.strftime('%Y-%m-%d %H:%M:%S')}

Stats:
- Self-upgrades applied: {upgrade_count}
- Self-questions answered: {question_count}
- Last post: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(state.get('last_post', 0))) if state.get('last_post') else 'never'}
- Last upgrade: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(state['self_upgrades'][-1]['ts'])) if state.get('self_upgrades') else 'none'}

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

    creds = load_credentials()
    cfg = load_runner_config()
    state = load_state()

    # Fail fast if OpenAI key is missing
    openai_key = llm.load_openai_key()
    openai_client = llm.make_client(openai_key)
    import os
    log.info("Moltbook runner starting (dry_run=%s)", cfg.dry_run)
    _notify(cfg, state, f"Moltbook runner started (dry_run={cfg.dry_run}, PID={os.getpid()})")

    consecutive_errors = 0

    while not _shutdown_event.is_set():
        try:
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
            if cfg.enable_auto_comment and cfg.keyword_allowlist:
                comments_this_cycle = 0
                last_comment_time = state.get("last_comment_time")

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

                    text = f"{post.get('title', '')} {post.get('content', '')}".lower()
                    if not any(k.lower() in text for k in cfg.keyword_allowlist):
                        continue

                    comment_text = llm.generate_comment(
                        openai_client,
                        post.get("title", ""),
                        post.get("content", ""),
                    )
                    if comment_text is None:
                        log.warning("LLM failed to generate comment for post %s", post.get("id"))
                        continue

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

                    state.setdefault("comment_history", []).append(
                        {
                            "post_id": post.get("id"),
                            "title": post.get("title", ""),
                            "content": post.get("content", ""),
                            "comment": comment_text,
                            "ts": int(time.time()),
                        }
                    )

                    comments_this_cycle += 1
                    state["last_comment_time"] = int(time.time())
                    last_comment_time = state["last_comment_time"]

            for post in new_posts:
                pid = post.get("id")
                if pid:
                    seen.add(pid)

            state["seen_post_ids"] = list(seen)[-500:]
            state["last_check"] = int(time.time())

            # -- Self-questioning with LLM answers --
            last_sq = state.get("last_self_question")
            now = int(time.time())
            did_self_question = False
            latest_answer = None
            latest_area = None

            if last_sq is None or now - int(last_sq) >= cfg.self_question_hours * 3600:
                questions = get_questions_with_codebase()
                question, idx = choose_question(state, questions)
                answer = llm.answer_question(openai_client, question.question)
                record_question(state, question, answer=answer)
                state["last_self_question"] = now
                state["self_question_index"] = idx + 1
                log.info("[self-question] %s: %s", question.area, question.question)
                if answer:
                    log.info("[self-answer] %s", answer)
                    did_self_question = True
                    latest_answer = answer
                    latest_area = question.area
                    _notify(
                        cfg,
                        state,
                        f"Q [{question.area}]: {_shorten(question.question, 120)}",
                    )

            # -- Auto-posting based on self-reflection --
            if cfg.enable_auto_post and cfg.post_after_self_question and did_self_question and latest_answer:
                last_post_time = state.get("last_post")
                can_post = (
                    last_post_time is None or
                    (now - int(last_post_time)) >= cfg.min_post_interval_hours * 3600
                )

                if can_post:
                    post_data = llm.generate_post(openai_client, latest_answer, latest_area)
                    if post_data and "title" in post_data and "content" in post_data:
                        if cfg.dry_run:
                            log.info(
                                "[dry-run] Would create post:\nTitle: %s\nContent: %s",
                                post_data["title"],
                                post_data["content"][:200],
                            )
                        else:
                            try:
                                result = create_post(
                                    creds.api_key,
                                    cfg.default_submolt,
                                    post_data["title"],
                                    content=post_data["content"],
                                )
                                state["last_post"] = now
                                log.info(
                                    "[auto-post] Created post: %s (id: %s)",
                                    post_data["title"],
                                    result.get("id"),
                                )
                                post_url = _post_url(result.get("id"))
                                _notify(
                                    cfg,
                                    state,
                                    f"Posted: {_shorten(post_data['title'], 120)}"
                                    + (f"\n{post_url}" if post_url else ""),
                                )
                            except Exception:
                                log.exception("Failed to create autonomous post")
                    else:
                        log.warning("LLM failed to generate valid post data")
                else:
                    log.debug(
                        "Skipping post: min interval not elapsed (%dh since last)",
                        (now - int(last_post_time)) // 3600 if last_post_time else 0,
                    )

            # -- Comment-based self-upgrades --
            config_was_modified = False
            if cfg.enable_comment_based_upgrades and cfg.enable_self_modification:
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

            # -- Self-improvement cycle --
            if cfg.enable_self_improvement:
                last_improvement = state.get("last_improvement_attempt")
                should_improve = (
                    last_improvement is None or
                    (now - int(last_improvement)) >= cfg.improvement_interval_hours * 3600
                )

                if should_improve:
                    try:
                        from .improvement import run_improvement_cycle
                        from .config import SafetyConfig
                        from . import git_ops as _git_ops

                        safety = SafetyConfig()

                        # Skip if open PRs exist
                        if not _git_ops.has_open_improvement_prs(
                            _git_ops.Path(__file__).resolve().parents[2]
                        ):
                            log.info("[self-improve] Starting improvement cycle...")
                            imp_result = run_improvement_cycle(
                                openai_client, state, safety,
                                model=cfg.improvement_model,
                                dry_run=cfg.dry_run,
                            )
                            state["last_improvement_attempt"] = now

                            if imp_result:
                                log.info(
                                    "[self-improve] Result: [%s] %s",
                                    imp_result.status,
                                    imp_result.task.description,
                                )
                                if imp_result.pr_url:
                                    _notify(
                                        cfg, state,
                                        f"PR: {imp_result.task.description[:100]}\n"
                                        f"{imp_result.pr_url}",
                                    )
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

            # -- Community-assisted improvement --
            if cfg.enable_community_improvement:
                try:
                    from .community_improvement import step_community_improvement, clear_community_improvement
                    from .config import SafetyConfig as _SafetyConfig

                    ci_safety = _SafetyConfig()
                    ci_result = step_community_improvement(
                        openai_client, state, creds, cfg, ci_safety,
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
            _interruptible_sleep(cfg.interval_seconds)

    log.info("Moltbook runner stopped.")
    _notify(cfg, state, f"Moltbook runner stopped. (PID={os.getpid()})")
    save_state(state)
    return 0
