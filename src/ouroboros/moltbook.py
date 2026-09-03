import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import lifecycle
from .model_defaults import DEFAULT_OPENAI_MODEL
from .retry import is_idempotent_request, retry_with_backoff
from .storage import load_json_file, save_json_file

log = logging.getLogger(__name__)

API_BASE = "https://www.moltbook.com/api/v1"
WEB_BASE = "https://www.moltbook.com"

# Aliased to the shared lifecycle event so llm's retry backoff observes the
# same shutdown. Existing references to _shutdown_event keep working.
_shutdown_event = lifecycle.shutdown_event

MAX_SELF_QUESTION_LOG = 200
MAX_BACKOFF_SECONDS = 900  # 15 min cap
MAX_SEEN_POST_IDS = 200
# Size of the recent-comment window kept in memory. Comments themselves are
# stored in SQLite without a limit; this only bounds what load_state hydrates,
# since every reader looks at a tail slice or the last seven days.
MAX_COMMENT_HISTORY = 100

# Set by load_state. save_state only drops comment_history once the records
# are known to be in the database; it is stripped from what gets written.
_COMMENTS_IN_SQLITE = "_comments_in_sqlite"
_COMMENT_MIGRATION = "comment_history_v1"
MAX_SELF_UPGRADES = 50
MAX_COMMUNITY_HISTORY = 20
MAX_KNOWLEDGE_PENDING = 200
KNOWLEDGE_BATCH_SIZE = 5
# A batch that always fails sits at the head of the queue forever, and nothing
# behind it is ever extracted. Bound the retries rather than the queue.
MAX_EXTRACTION_ATTEMPTS = 3
MAX_PENDING_CONTENT_CHARS = 2000


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
    return load_json_file(path)


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


def _send(req: "urllib.request.Request") -> Dict[str, Any]:
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


_send_with_retry = retry_with_backoff(cancelled=lifecycle.is_shutting_down)(_send)


def _urlopen_json(req: "urllib.request.Request") -> Dict[str, Any]:
    """Send req and decode the JSON body.

    Transient failures are retried only for idempotent methods. A POST that
    the server committed but whose response was lost is indistinguishable
    here from one it never received, and this API creates public posts and
    comments -- replaying one would duplicate it. There is no idempotency key
    to make that safe, so writes get a single attempt.

    Retries live at this level rather than in _request so the MoltbookError
    wrapper does not hide the original exception type from is_retryable.
    """
    if is_idempotent_request(req):
        return _send_with_retry(req)
    return _send(req)


def _request(method: str, path: str, api_key: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{API_BASE}{path}"
    data = None
    headers = {"Authorization": f"Bearer {api_key}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        return _urlopen_json(req)
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


def _author_name(record: Dict[str, Any]) -> Optional[str]:
    """Author name of a feed record, or None when the author is malformed.

    `record.get("author", {})` only falls back when the key is absent: an
    explicit JSON null hands back None, and a bare string author is not a
    mapping either. Both used to raise AttributeError here and abort the
    whole poll over one bad record.
    """
    author = record.get("author")
    if not isinstance(author, dict):
        return None
    name = author.get("name")
    return name if isinstance(name, str) else None


def get_my_posts(api_key: str, agent_name: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Fetch posts authored by this agent."""
    feed = get_feed(api_key, sort="new", limit=50)
    posts = feed.get("posts", [])
    return [p for p in posts if _author_name(p) == agent_name][:limit]


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
    enable_auto_post: bool = True
    post_after_self_question: bool = True
    min_post_interval_hours: int = 12
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
    # Mirrors SafetyConfig.max_improvements_per_day so the tracked config can
    # actually set it. Counts attempts in a rolling 24h window, not merges.
    # The default matches SafetyConfig's, so an unset config is unchanged.
    max_improvements_per_day: int = 3
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
    # Read via getattr in community_improvement and set in the tracked
    # agent.json, but never declared here, so nothing validated it.
    community_post_interval_hours: float = 1.0
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
    # Local CLI agent backends for the self-improvement engine.
    # "openai" (default), "claude", "codex", or "agy". See backends.py.
    identify_backend: str = "openai"
    plan_backend: str = "openai"
    generator_backend: str = "openai"
    reviewer_backend: str = "openai"
    generator_model: str = ""
    # Reviewer routing, independent of the generation model. Empty
    # reviewer_model means "use improvement_model", which is what the loop did
    # unconditionally before these were configurable. Setting reviewer_base_url
    # points the review step at an OpenAI-compatible endpoint (e.g. Ollama
    # Cloud) while generation stays on the default backend.
    reviewer_model: str = ""
    reviewer_base_url: str = ""
    reviewer_api_key: Optional[str] = field(default=None, repr=False)


def _warn_unknown_config_keys(data: Dict[str, Any], path: str) -> None:
    """Name every agent.json key that is not a setting at all.

    Every setting below is pulled out by name, so a typo, a stale key, or a
    SafetyConfig cap that is a compile-time constant parses cleanly and then
    does nothing: the operator sees a saved file and keeps the old limit.
    config_schema was written to stop exactly that, and was wired to
    `config set` and to comment suggestions but not to the file the README
    calls the runtime config.

    A warning, not a refusal. The agent runs unattended, and one unrecognised
    key must not keep the rest of a valid config from starting.
    """
    from . import config_schema

    for key in data:
        error = config_schema.unknown_key_error(key)
        if error:
            log.warning("%s: %s -- ignored, this setting has no effect", path, error)


def load_runner_config() -> RunnerConfig:
    cfg_path = os.path.expanduser("~/.config/moltbook/agent.json")
    data: Dict[str, Any] = {}
    if os.path.exists(cfg_path):
        data = _read_json_file(cfg_path)
        _warn_unknown_config_keys(data, cfg_path)

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
    # Reviewer credential. ollama_api_key is accepted because the reviewer
    # backend this was added for is Ollama Cloud; reviewer_api_key is the
    # backend-neutral name for any other OpenAI-compatible gateway.
    reviewer_api_key = (
        os.environ.get("REVIEWER_API_KEY")
        or os.environ.get("OLLAMA_API_KEY")
        or cred_data.get("reviewer_api_key")
        or cred_data.get("ollama_api_key")
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
        enable_auto_post=bool(data.get("enable_auto_post", True)),
        post_after_self_question=bool(data.get("post_after_self_question", True)),
        min_post_interval_hours=int(data.get("min_post_interval_hours", 12)),
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
        max_improvements_per_day=int(data.get("max_improvements_per_day", 3)),
        improvement_model=str(data.get("improvement_model", DEFAULT_OPENAI_MODEL)),
        improvement_types=data.get("improvement_types"),
        enable_auto_issue_creation=bool(data.get("enable_auto_issue_creation", True)),
        enable_auto_merge=bool(data.get("enable_auto_merge", False)),
        enable_community_improvement=bool(data.get("enable_community_improvement", False)),
        community_wait_hours=int(data.get("community_wait_hours", 48)),
        community_min_comments_for_early=int(data.get("community_min_comments_for_early", 3)),
        community_improvement_interval_hours=int(data.get("community_improvement_interval_hours", 72)),
        community_post_interval_hours=float(data.get("community_post_interval_hours", 1.0)),
        enable_github_improvement=bool(data.get("enable_github_improvement", False)),
        github_improvement_interval_hours=int(data.get("github_improvement_interval_hours", 12)),
        enable_issue_scouting=bool(data.get("enable_issue_scouting", False)),
        issue_scouting_interval_hours=int(data.get("issue_scouting_interval_hours", 24)),
        issue_scouting_model=str(data.get("issue_scouting_model", data.get("improvement_model", DEFAULT_OPENAI_MODEL))),
        enable_wiki=bool(data.get("enable_wiki", False)),
        wiki_update_interval_hours=int(data.get("wiki_update_interval_hours", 24)),
        identify_backend=str(data.get("identify_backend", "openai")),
        plan_backend=str(data.get("plan_backend", "openai")),
        generator_backend=str(data.get("generator_backend", "openai")),
        reviewer_backend=str(data.get("reviewer_backend", "openai")),
        generator_model=str(data.get("generator_model", "")),
        reviewer_model=str(data.get("reviewer_model") or ""),
        reviewer_base_url=str(data.get("reviewer_base_url") or ""),
        reviewer_api_key=reviewer_api_key,
    )


def _state_path() -> str:
    return os.path.expanduser("~/.config/moltbook/state.json")


def _default_state() -> Dict[str, Any]:
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
        "knowledge_pending": [],
        "community_improvement": None,
        "community_improvement_history": [],
        "last_community_improvement_start": None,
        "last_issue_scouting_attempt": None,
    }


def load_state() -> Dict[str, Any]:
    # No os.path.exists() preflight: it answers False for any stat error, so a
    # transient EACCES would look like a fresh install. run_loop persists what
    # it loaded, so that would overwrite seen_post_ids and the cycle
    # timestamps with defaults and the agent would repeat work it had already
    # done. load_json_file distinguishes missing from unreadable.
    path = _state_path()
    state = load_json_file(
        path,
        default=_default_state(),
        error_msg=f"Corrupt state file at {path}, returning default",
        logger=log,
    )

    # comment_history lives in SQLite. Hydrate a recent window so the readers
    # that slice state["comment_history"] keep working unchanged, while the
    # full history stays in the database and out of this file -- it was 102 KB
    # of a 169 KB state.json, rewritten every cycle.
    try:
        storage = _comment_storage()
        _drain_comment_outbox(state, storage)
        _drain_knowledge_outbox(state)
        legacy = state.get("comment_history") or []
        if storage.migration_done(_COMMENT_MIGRATION):
            legacy = []
        if legacy:
            # save_state stops writing this list, so the next save would erase
            # it. Snapshot before that, without depending on the startup
            # migration having run first.
            from .state_migration import freeze_rollback_snapshot

            snapshot = freeze_rollback_snapshot(Path(path))
            if snapshot is None:
                # No rollback artifact means no cutover. Leave the file
                # authoritative and try again next start.
                log.warning("Skipping comment cutover: could not snapshot %s", path)
                state[_COMMENTS_IN_SQLITE] = False
                return state
            imported = 0
            failed = 0
            for c in legacy:
                if not isinstance(c, dict):
                    continue
                try:
                    imported += bool(storage.append_comment(c))
                except Exception:
                    failed += 1
            if failed:
                # save_state would drop the list next, so a partial import
                # would lose the records that did not make it.
                log.warning(
                    "Skipping comment cutover: %d of %d records could not be "
                    "imported", failed, len(legacy),
                )
                state[_COMMENTS_IN_SQLITE] = False
                return state
            # Only now is the file safe to stop writing.
            storage.mark_migration_done(_COMMENT_MIGRATION)
            if imported:
                log.info("Imported %d comments from state.json into SQLite", imported)
        state["comment_history"] = storage.get_comment_history(limit=MAX_COMMENT_HISTORY)
        state[_COMMENTS_IN_SQLITE] = True
    except Exception:
        # Fall back to whatever the file had rather than losing the window.
        log.warning("Could not load comment history from storage", exc_info=True)
        state[_COMMENTS_IN_SQLITE] = False

    return state


def save_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    _trim_state(state)
    # comment_history is owned by SQLite once the cutover succeeded; writing
    # it here too would recreate the bloat and give two sources of truth. If
    # the cutover was skipped -- no snapshot, or a record that would not
    # import -- the file is still authoritative and must keep the list.
    cut_over = state.get(_COMMENTS_IN_SQLITE, False)
    to_write = {
        k: v for k, v in state.items()
        if k != _COMMENTS_IN_SQLITE and not (cut_over and k == "comment_history")
    }
    save_json_file(path, to_write, sort_keys=True)


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


def _comment_storage() -> Any:
    from .storage import OuroborosStorage

    return OuroborosStorage()


def record_comment(state: Dict[str, Any], entry: Dict[str, Any]) -> None:
    """Persist a posted comment and make it visible to this cycle's readers.

    The comment is already public by the time this runs, so a database
    failure must not lose the local record. On failure the entry goes to an
    outbox that save_state does persist, and load_state retries it -- without
    that it would live only in the in-memory list, which save_state
    deliberately drops.
    """
    try:
        _comment_storage().append_comment(entry)
    except Exception:
        log.warning("Could not persist comment; queued for retry", exc_info=True)
        state.setdefault("comment_outbox", []).append(entry)
        # Written now, not at the end of the cycle: the comment is already
        # public, and a crash before the cycle-end save would lose the only
        # local record of it.
        try:
            save_state(state)
        except Exception:
            log.error("Could not persist the comment outbox", exc_info=True)
    state.setdefault("comment_history", []).append(entry)


def _queue_for_extraction(state: Dict[str, Any], posts: List[Dict[str, Any]]) -> int:
    """Queue posts for knowledge extraction. Returns how many were added.

    seen_post_ids answers "already displayed"; this answers "already
    considered for an insight". One list did both, so the per-cycle LLM budget
    silently doubled as a cap on how many posts were ever looked at: with ten
    posts a fetch, the sixth uncommented one was checkpointed as seen without
    reaching extraction, and later cycles filter on seen, so it never came
    back.
    """
    pending = state.setdefault("knowledge_pending", [])
    known = {entry.get("id") for entry in pending}
    added = 0
    for post in posts:
        pid = post.get("id")
        if not pid or pid in known:
            continue
        known.add(pid)
        pending.append({
            "id": pid,
            # `or ""`, not a get default: an explicit null title would
            # otherwise reach the prompt as the literal "None".
            "title": post.get("title") or "",
            "content": (post.get("content") or "")[:MAX_PENDING_CONTENT_CHARS],
            "attempts": 0,
        })
        added += 1
    # The cap is enforced in _trim_state, at save time. Evicting here would
    # drop the head of the queue -- the exact batch about to be drained.
    return added


def _drain_extraction_queue(
    state: Dict[str, Any], client: Any, limit: int = KNOWLEDGE_BATCH_SIZE
) -> "tuple[List[Dict[str, Any]], List[Any]]":
    """Extract insights for the head of the queue.

    Returns (entries, processed_ids). The caller records the entries and then
    calls _release_extracted with the ids -- the queue is not the record, so a
    post may only leave it once its insight is somewhere durable.

    A post leaves the queue once it has a decision: an insight, or a batch
    that ran and found none. extract_insights_batch returns None only when the
    call failed, so a transient failure leaves the posts queued rather than
    dropping them.
    """
    from . import llm  # local, matching the rest of this module

    pending = state.get("knowledge_pending") or []
    if not pending:
        return [], []

    for entry in pending:
        # The loop is single-threaded, so nothing is legitimately in flight
        # when a drain starts. Clearing first means a mark left behind by an
        # interrupted cycle cannot outlive it.
        entry.pop("in_flight", None)

    batch = pending[:limit]
    for entry in batch:
        # _record_knowledge saves state on a write failure, and save_state
        # trims -- with the batch still queued, since release comes after
        # recording. Nothing in flight may be evicted underneath us.
        entry["in_flight"] = True
    try:
        insights = llm.extract_insights_batch(client, batch)
    except Exception:
        # extract_insights_batch returns None on failure today, but the
        # queue's guarantee that it cannot wedge should not depend on that
        # staying true: an escaping exception would leave attempts at zero and
        # retry the same head forever.
        log.warning("[knowledge-base] Extraction raised", exc_info=True)
        insights = None

    if not isinstance(insights, list):
        # None is the failure sentinel; anything else non-list means the reply
        # did not parse, and either way we cannot say these posts were read.
        for entry in batch:
            entry["attempts"] = int(entry.get("attempts", 0) or 0) + 1
        for entry in batch:
            entry.pop("in_flight", None)
        spent = [e for e in batch if e["attempts"] >= MAX_EXTRACTION_ATTEMPTS]
        if spent:
            ids = {e.get("id") for e in spent}
            state["knowledge_pending"] = [
                e for e in pending if e.get("id") not in ids
            ]
            log.warning(
                "[knowledge-base] Giving up on %d post(s) after %d attempts: %s",
                len(spent), MAX_EXTRACTION_ATTEMPTS,
                ", ".join(str(e.get("id")) for e in spent),
            )
        else:
            log.info(
                "[knowledge-base] Extraction failed; %d post(s) stay queued",
                len(batch),
            )
        return [], []

    entries: List[Dict[str, Any]] = []
    now = int(time.time())
    for item in insights:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("post_index", -1))
        except (TypeError, ValueError):
            continue
        if not 0 <= idx < len(batch):
            continue
        insight = item.get("insight") or ""
        if not insight:
            continue
        entries.append({
            "post_id": batch[idx].get("id"),
            "post_title": batch[idx].get("title", ""),
            "insight": insight,
            "tags": item.get("tags", []),
            "ts": now,
            "source": "extraction",
        })

    log.info(
        "[knowledge-base] Extracted %d insight(s) from %d post(s), %d queued",
        len(entries), len(batch), len(pending) - len(batch),
    )
    return entries, [e.get("id") for e in batch]


def _release_extracted(state: Dict[str, Any], post_ids: List[Any]) -> None:
    """Drop posts from the queue once their insights are accounted for.

    Called after _record_knowledge, which either persists the batch or queues
    it in the durable outbox -- so by then the insight cannot be lost. Removing
    them inside the drain instead left a window where an exception between
    extracting and recording lost both.
    """
    # A None in here would match every entry whose id is missing and purge
    # them all. _queue_for_extraction never admits one, but the invariant
    # belongs where the deletion happens.
    done = {pid for pid in post_ids if pid is not None}
    if not done:
        return
    state["knowledge_pending"] = [
        entry for entry in state.get("knowledge_pending", [])
        if entry.get("id") not in done
    ]


def _record_knowledge(
    state: Dict[str, Any],
    entries: List[Dict[str, Any]],
    processed_ids: Optional[List[Any]] = None,
) -> None:
    """Persist knowledge entries, queueing the batch if the write fails.

    Releases processed_ids from the extraction queue in the same step. The two
    have to be one step: the failure path saves state, and if the queue still
    held the posts at that moment, a crash straight after would leave the disk
    holding both the insights in the outbox and the posts still pending --
    extracting and recording them a second time on restart.
    """
    from .knowledge_base import add_entries

    try:
        add_entries(entries)
        log.info("[knowledge-base] Added %d entries", len(entries))
        _release_extracted(state, processed_ids or [])
    except Exception:
        log.warning("Could not persist knowledge entries; queued", exc_info=True)
        state.setdefault("knowledge_outbox", []).extend(entries)
        # The outbox owns them now, so the queue must let go before the save.
        _release_extracted(state, processed_ids or [])
        try:
            save_state(state)
        except Exception:
            log.error("Could not persist the knowledge outbox", exc_info=True)


def _drain_knowledge_outbox(state: Dict[str, Any]) -> None:
    """Retry a knowledge batch whose write failed on an earlier cycle."""
    queued = state.get("knowledge_outbox") or []
    if not queued:
        return
    from .knowledge_base import add_entries

    try:
        add_entries(queued)
    except Exception:
        return  # still failing; keep it queued
    state["knowledge_outbox"] = []
    log.info("Recovered %d queued knowledge entries", len(queued))


def _drain_comment_outbox(state: Dict[str, Any], storage: Any) -> None:
    """Retry comments whose database write failed on an earlier cycle."""
    outbox = state.get("comment_outbox") or []
    if not outbox:
        return
    remaining = []
    for entry in outbox:
        try:
            storage.append_comment(entry)
        except Exception:
            remaining.append(entry)
    state["comment_outbox"] = remaining
    if len(remaining) < len(outbox):
        log.info("Recovered %d queued comments into storage", len(outbox) - len(remaining))


def _trim_comment_history(state: Dict[str, Any], limit: int = 80) -> None:
    """Bound the in-memory window. SQLite keeps the full history."""
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

    queued = state.get("knowledge_pending", [])
    if len(queued) > MAX_KNOWLEDGE_PENDING:
        # Two kinds of entry are never candidates. A batch mid-extraction is
        # about to be released, and evicting it would lose an insight already
        # paid for. A batch part-way through its retries sits at the head, so
        # the cap would drop it before the next attempt -- voiding the
        # three-attempt rule precisely when it matters, since a sustained
        # extraction outage is what saturates the queue in the first place.
        #
        # Only the head batch is ever retried, so this can protect at most one
        # batch; the slice makes that a bound rather than an assumption.
        def _protected(entry):
            return entry.get("in_flight") or entry.get("attempts")

        keep = [e for e in queued if _protected(e)][:KNOWLEDGE_BATCH_SIZE]
        held = {id(e) for e in keep}
        rest = [e for e in queued if id(e) not in held]
        room = max(0, MAX_KNOWLEDGE_PENDING - len(keep))
        dropped = rest[:-room] if room else rest
        if dropped:
            # Named, not silent: a dropped post is one the agent never learns
            # from, which is the bug this queue exists to fix. The oldest go
            # first -- the drain already took the head this cycle, and by the
            # time the cap bites, a fresh post is worth more than one from
            # twenty hours ago.
            log.warning(
                "[knowledge-base] Queue over %d; dropped %d oldest: %s",
                MAX_KNOWLEDGE_PENDING, len(dropped),
                ", ".join(str(e.get("id")) for e in dropped),
            )
        state["knowledge_pending"] = keep + (rest[-room:] if room else [])

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

    # Backfill the JSON history into SQLite. Idempotent and non-destructive,
    # so it is safe to run on every start and needs no separate deploy step.
    try:
        from .codebase import get_repo_root
        from .state_migration import migrate_json_history

        migrate_json_history(get_repo_root())
    except Exception:
        # A failed backfill must not stop the agent; the JSON files are still
        # there and the next start will try again.
        log.warning("State migration skipped", exc_info=True)

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

    def _pick_client(model: str) -> Any:
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

                        _comment_client = openai_client
                        comment_text = llm.generate_comment(
                            _comment_client,
                            post.get("title", ""),
                            post.get("content", ""),
                            model=DEFAULT_OPENAI_MODEL,
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
                        record_comment(state, comment_entry)

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

                # Nothing may save state between here and the extraction
                # queueing below: a post recorded as seen but not yet queued is
                # a post that is never extracted, which is the bug #67 is
                # about. They reach disk in the same write.
                #
                # Insertion order matters: the cap is meant to keep the most
                # recent ids, and list(set) order is arbitrary -- trimming that
                # dropped ids at random, so old posts resurfaced as new. The
                # cap also disagreed with _trim_state's, which then reapplied
                # its own arbitrary slice.
                seen_order = list(state.get("seen_post_ids", []))
                for post in new_posts:
                    pid = post.get("id")
                    if pid and pid not in seen:
                        seen.add(pid)
                        seen_order.append(pid)

                state["seen_post_ids"] = seen_order[-MAX_SEEN_POST_IDS:]
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
                # Drain even with no new posts: the queue outlives the cycle
                # that filled it, which is the whole point of having one.
                if cfg.enable_knowledge_base and (
                    new_posts or state.get("knowledge_pending")
                ):
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

                        # Everything not commented on joins a durable queue.
                        # The per-cycle LLM budget now bounds how many are
                        # processed per pass, not how many are ever considered.
                        _queue_for_extraction(state, [
                            p for p in new_posts
                            if p.get("id") not in commented_post_ids
                        ])
                        extracted, processed_ids = _drain_extraction_queue(
                            state, openai_client
                        )
                        kb_entries.extend(extracted)

                        if kb_entries:
                            # Releases the batch itself, once the entries are
                            # either written or in the durable outbox.
                            _record_knowledge(state, kb_entries, processed_ids)
                        else:
                            # A batch that ran and found nothing is still a
                            # decision, so the posts still leave the queue.
                            _release_extracted(state, processed_ids)
                        # Checkpoint unconditionally rather than waiting for
                        # the end of the cycle. A crash before this would
                        # re-extract a batch already recorded, paying for the
                        # call twice; it would also roll back the failure
                        # bookkeeping -- attempt counts and posts given up on --
                        # so a poisoned batch would get its three tries again
                        # on every restart. This write is also what puts the
                        # queue on disk alongside the seen_post_ids that
                        # decided what entered it.
                        try:
                            save_state(state)
                        except Exception:
                            log.warning(
                                "Could not checkpoint the extraction queue",
                                exc_info=True,
                            )
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
                            _odds_client = openai_client
                            _odds_model = DEFAULT_OPENAI_MODEL
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
                _sq_client = openai_client
                _sq_model = DEFAULT_OPENAI_MODEL
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

                    # -- Auto-posting --
                    if cfg.enable_auto_post and cfg.post_after_self_question:
                        last_post = state.get("last_post")
                        should_post = (
                            last_post is None or
                            (now - int(last_post)) >= cfg.min_post_interval_hours * 3600
                        )

                        if should_post:
                            try:
                                post_data = llm.generate_post(
                                    _sq_client,
                                    answer,
                                    question.area,
                                    model=_sq_model,
                                    extra_context=_comment_codebase_ctx,
                                )
                                if post_data:
                                    post_result = create_post(
                                        creds.api_key,
                                        cfg.default_submolt,
                                        post_data["title"],
                                        content=post_data["content"],
                                    )
                                    state["last_post"] = now
                                    log.info("[auto-post] Created post: %s", post_result.get("id"))
                                    _notify(
                                        cfg,
                                        state,
                                        f"New post: {post_data['title']}\n"
                                        f"URL: {_post_url(post_result.get('id'))}",
                                    )
                            except Exception:
                                log.exception("Auto-posting failed")

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
                                            from .self_modify import (
                                                filter_untrusted_config_updates,
                                                modify_runner_config,
                                            )

                                            # These come from a public comment.
                                            config_changes, _rejected = (
                                                filter_untrusted_config_updates(config_changes)
                                            )
                                            if _rejected:
                                                log.warning(
                                                    "[self-upgrade] Refused operator-only keys "
                                                    "from %s: %s",
                                                    suggestion.get("commenter", "unknown"),
                                                    ", ".join(_rejected),
                                                )
                                        if config_changes:
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
                        from .config import SafetyConfig, reviewer_safety_kwargs as _reviewer_safety_kwargs

                        safety = SafetyConfig(
                            enable_auto_merge=cfg.enable_auto_merge,
                            max_improvements_per_day=getattr(cfg, "max_improvements_per_day", 3),
                            identify_backend=getattr(cfg, "identify_backend", "openai"),
                            plan_backend=getattr(cfg, "plan_backend", "openai"),
                            generator_backend=getattr(cfg, "generator_backend", "openai"),
                            generator_model=getattr(cfg, "generator_model", "") or None,
                            **_reviewer_safety_kwargs(cfg),
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
                        if _git_ops.has_open_improvement_prs(repo_root) is False:
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
