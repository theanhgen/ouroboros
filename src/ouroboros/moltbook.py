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


@dataclass
class RunnerConfig:
    interval_seconds: int = 1800
    enable_auto_post: bool = False
    enable_auto_comment: bool = False
    keyword_allowlist: Optional[List[str]] = None
    default_submolt: str = "general"
    dry_run: bool = True
    self_question_hours: int = 8
    max_comments_per_cycle: int = 3
    min_comment_interval_seconds: int = 300


def load_runner_config() -> RunnerConfig:
    cfg_path = os.path.expanduser("~/.config/moltbook/agent.json")
    if not os.path.exists(cfg_path):
        return RunnerConfig()
    data = _read_json_file(cfg_path)
    return RunnerConfig(
        interval_seconds=int(data.get("interval_seconds", 1800)),
        enable_auto_post=bool(data.get("enable_auto_post", False)),
        enable_auto_comment=bool(data.get("enable_auto_comment", False)),
        keyword_allowlist=data.get("keyword_allowlist"),
        default_submolt=data.get("default_submolt", "general"),
        dry_run=bool(data.get("dry_run", True)),
        self_question_hours=int(data.get("self_question_hours", 8)),
        max_comments_per_cycle=int(data.get("max_comments_per_cycle", 3)),
        min_comment_interval_seconds=int(data.get("min_comment_interval_seconds", 300)),
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
            "last_comment_time": None,
            "self_question_index": 0,
            "self_question_log": [],
            "seen_post_ids": [],
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


def _trim_self_question_log(state: Dict[str, Any]) -> None:
    log_list = state.get("self_question_log", [])
    if len(log_list) > MAX_SELF_QUESTION_LOG:
        state["self_question_log"] = log_list[-MAX_SELF_QUESTION_LOG:]


def _interruptible_sleep(seconds: int) -> None:
    """Sleep that returns early when shutdown is requested."""
    log.debug("Sleeping %ds (next cycle)", seconds)
    _shutdown_event.wait(timeout=seconds)


def run_loop() -> int:
    from . import llm
    from .self_question import DEFAULT_QUESTIONS, choose_question, record_question

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    _shutdown_event.clear()

    creds = load_credentials()
    cfg = load_runner_config()
    state = load_state()

    # Fail fast if OpenAI key is missing
    openai_key = llm.load_openai_key()
    openai_client = llm.make_client(openai_key)
    log.info("Moltbook runner starting (dry_run=%s)", cfg.dry_run)

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
                        create_comment(creds.api_key, post.get("id"), comment_text)
                        log.info("Commented on post %s", post.get("id"))

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
            if last_sq is None or now - int(last_sq) >= cfg.self_question_hours * 3600:
                question, idx = choose_question(state, DEFAULT_QUESTIONS)
                answer = llm.answer_question(openai_client, question.question)
                record_question(state, question, answer=answer)
                state["last_self_question"] = now
                state["self_question_index"] = idx + 1
                log.info("[self-question] %s: %s", question.area, question.question)
                if answer:
                    log.info("[self-answer] %s", answer)

            _trim_self_question_log(state)
            save_state(state)
            consecutive_errors = 0

        except Exception:
            consecutive_errors += 1
            backoff = min(cfg.interval_seconds * (2 ** (consecutive_errors - 1)), MAX_BACKOFF_SECONDS)
            log.exception("Error in run_loop cycle (%d consecutive). Backing off %ds.", consecutive_errors, backoff)
            if not _shutdown_event.is_set():
                _interruptible_sleep(backoff)
            continue

        if not _shutdown_event.is_set():
            _interruptible_sleep(cfg.interval_seconds)

    log.info("Moltbook runner stopped.")
    save_state(state)
    return 0
