import json
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

API_BASE = "https://www.moltbook.com/api/v1"


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
    )


def _state_path() -> str:
    return os.path.expanduser("~/.config/moltbook/state.json")


def load_state() -> Dict[str, Any]:
    path = _state_path()
    if not os.path.exists(path):
        return {"last_check": None, "last_post": None, "seen_post_ids": []}
    return _read_json_file(path)


def save_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def run_loop() -> int:
    creds = load_credentials()
    cfg = load_runner_config()
    state = load_state()

    while True:
        status = get_status(creds.api_key)
        if status.get("status") != "claimed":
            print("Not claimed yet. Sleeping.")
            time.sleep(cfg.interval_seconds)
            continue

        feed = get_feed(creds.api_key, sort="new", limit=10)
        posts = feed.get("posts") or feed.get("data") or []

        seen = set(state.get("seen_post_ids", []))
        new_posts = [p for p in posts if p.get("id") and p.get("id") not in seen]

        if cfg.enable_auto_comment and cfg.keyword_allowlist:
            for post in new_posts:
                text = f"{post.get('title','')} {post.get('content','')}".lower()
                if any(k.lower() in text for k in cfg.keyword_allowlist):
                    if cfg.dry_run:
                        print(f"[dry-run] Would comment on {post.get('id')}")
                    else:
                        create_comment(creds.api_key, post.get("id"), "Interesting. Can you share more context?")

        for post in new_posts:
            pid = post.get("id")
            if pid:
                seen.add(pid)

        state["seen_post_ids"] = list(seen)[-500:]
        state["last_check"] = int(time.time())
        save_state(state)

        time.sleep(cfg.interval_seconds)


