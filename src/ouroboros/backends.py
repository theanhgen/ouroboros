"""Local CLI agent backends (claude, codex) as alternatives to the OpenAI API.

Two integration modes:

  1. Completion shim -- ``CLIClient`` duck-types the OpenAI/Anthropic client
     surface that ``llm.py`` already uses (``.chat.completions.create`` and
     ``.messages.create``), so existing call sites (review, identify, social)
     work unchanged. The CLI is run in non-interactive "print" mode and its
     stdout is mapped back into a response object.

  2. Agent mode -- ``agent_generate_changes`` lets the CLI edit the working
     tree directly, then converts the resulting diff into ``CodeChange``
     objects and resets the tree. The caller's existing validate/apply/test/
     revert pipeline re-applies and gates those changes unchanged, so the
     safety caps and forbidden-path checks still apply to the agent's diff.

Notes:
  * The systemd service PATH does NOT include ``~/.local/bin`` or
    ``~/.npm-global/bin`` where these CLIs live, so binaries are resolved by
    absolute path -- never rely on PATH.
  * Both CLIs authenticate from the invoking user's home (``~/.claude`` /
    ``~/.codex``); the service runs as the same user so auth carries over.
  * codex routes to OpenAI under the hood and its stdout is human-decorated;
    it is supported but ``claude`` is the recommended default.
"""

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from .git_ops import (
    _decode_git_path,
    _git_porcelain_changes,
    _git_porcelain_target_path,
)

log = logging.getLogger(__name__)

CLI_BACKENDS = ("claude", "codex", "agy")

# Known install locations that are absent from the systemd service PATH.
_EXTRA_BIN_DIRS = (
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.npm-global/bin"),
)

_DEFAULT_TIMEOUT = 600


def resolve_binary(name: str) -> Optional[str]:
    """Return the absolute path to a CLI binary, or None if not found.

    Checks PATH first, then known install dirs that the systemd service PATH
    omits.
    """
    # Prefer the user's installed binaries (the versions actually tested) over
    # whatever is on PATH. The systemd service PATH can resolve a different,
    # older binary (e.g. an /usr/local/bin codex) that behaves differently.
    for d in _EXTRA_BIN_DIRS:
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return shutil.which(name)


def is_cli_backend(backend: Optional[str]) -> bool:
    """Return True if ``backend`` names a supported local CLI agent."""
    return backend in CLI_BACKENDS


def cli_available(backend: str) -> bool:
    """Return True if the backend is a CLI agent whose binary is installed."""
    return is_cli_backend(backend) and resolve_binary(backend) is not None


# --------------------------------------------------------------------------- #
# Prompt + output handling
# --------------------------------------------------------------------------- #

def _messages_to_prompt(system_prompt: str, user_prompt: str, want_json: bool) -> str:
    parts = []
    if system_prompt:
        parts.append(system_prompt)
    if user_prompt:
        parts.append(user_prompt)
    if want_json:
        parts.append("Respond with ONLY valid JSON. No markdown fences, no prose.")
    return "\n\n".join(p for p in parts if p)


def parse_claude_output(stdout: str) -> Tuple[str, Optional[Dict[str, int]]]:
    """Parse ``claude --output-format json`` stdout into (text, usage)."""
    data = json.loads(stdout)
    if data.get("is_error"):
        raise RuntimeError(f"claude reported error: {str(data.get('result', ''))[:300]}")
    text = data.get("result", "") or ""
    usage = None
    u = data.get("usage") or {}
    if u:
        usage = {
            "prompt_tokens": int(u.get("input_tokens", 0) or 0),
            "completion_tokens": int(u.get("output_tokens", 0) or 0),
        }
    return text, usage


def parse_agy_output(stdout: str) -> str:
    """Extract the response from ``agy --print`` stdout.

    agy prints the model's reply as plain text in print mode (no JSON envelope,
    no decorated transcript), so we just trim it. Callers that need JSON do
    brace-extraction on the result, which tolerates any stray prose/fences.
    """
    return stdout.strip()


def parse_codex_output(stdout: str) -> str:
    """Best-effort extraction of the final answer from ``codex exec`` stdout.

    codex prints a human-decorated transcript; the final assistant message
    follows the last bare ``codex`` marker line and precedes the ``tokens
    used`` footer. This heuristic is intentionally forgiving and falls back to
    the raw stdout when the markers are absent.
    """
    lines = stdout.splitlines()
    last_marker = -1
    for i, line in enumerate(lines):
        if line.strip() == "codex":
            last_marker = i
    if last_marker == -1:
        return stdout.strip()
    collected = []
    for line in lines[last_marker + 1:]:
        if line.strip().lower().startswith("tokens used"):
            break
        collected.append(line)
    result = "\n".join(collected).strip()
    return result or stdout.strip()


def _run_claude(
    binary: str,
    prompt: str,
    *,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    edit: bool = False,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Tuple[str, Optional[Dict[str, int]]]:
    cmd = [binary, "-p", prompt, "--output-format", "json"]
    if model and str(model).startswith("claude"):
        cmd += ["--model", model]
    if edit:
        cmd += ["--permission-mode", "acceptEdits"]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr[:500]}")
    return parse_claude_output(proc.stdout)


def _run_codex(
    binary: str,
    prompt: str,
    *,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    edit: bool = False,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Tuple[str, Optional[Dict[str, int]]]:
    cmd = [binary, "exec"]
    if edit:
        # `codex exec` is read-only by default; allow it to edit the working tree.
        cmd += ["--sandbox", "workspace-write"]
    # Only override the model when an explicit codex/openai model id is given.
    if model and str(model).startswith(("gpt", "o3", "o4", "codex")):
        cmd += ["-c", f'model="{model}"']
    cmd.append(prompt)
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"codex exited {proc.returncode}: {proc.stderr[:500]}")
    # codex does not expose token usage in a stable machine-readable form.
    return parse_codex_output(proc.stdout), None


def _run_agy(
    binary: str,
    prompt: str,
    *,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    edit: bool = False,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Tuple[str, Optional[Dict[str, int]]]:
    cmd = [binary, "-p", prompt]
    if model:
        cmd += ["--model", model]
    # A subprocess has no human to answer a permission prompt. agy DENIES and
    # exits 1 -- unlike `claude -p` / `codex exec`, which degrade to read-only.
    # That broke every planning call from 2026-08-01: agy tried to run
    # `pwd && ls -la`, was denied, exited 1, and the cycle logged it as the
    # unrelated "no plan generated".
    cmd += ["--dangerously-skip-permissions"]
    if edit:
        cmd += ["--mode", "accept-edits"]
        if cwd:
            cmd += ["--add-dir", cwd]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"agy exited {proc.returncode}: {proc.stderr[:500]}")
    # agy print mode does not expose token usage.
    return parse_agy_output(proc.stdout), None


# --------------------------------------------------------------------------- #
# Completion shim -- duck-types the OpenAI / Anthropic client
# --------------------------------------------------------------------------- #

class _Completions:
    def __init__(self, client: "CLIClient") -> None:
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        messages = kwargs.get("messages", []) or []
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
        user = "\n\n".join(m["content"] for m in messages if m.get("role") != "system")
        want_json = bool(kwargs.get("response_format")) or bool(kwargs.get("tools"))
        text, usage = self._client._invoke(system, user, want_json)
        msg = SimpleNamespace(content=text, tool_calls=None)
        usage_ns = None
        if usage:
            usage_ns = SimpleNamespace(
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["prompt_tokens"] + usage["completion_tokens"],
            )
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage_ns)


class _Chat:
    def __init__(self, client: "CLIClient") -> None:
        self.completions = _Completions(client)


class _Messages:
    def __init__(self, client: "CLIClient") -> None:
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        system = kwargs.get("system", "") or ""
        messages = kwargs.get("messages", []) or []
        user = "\n\n".join(m["content"] for m in messages)
        text, usage = self._client._invoke(system, user, False)
        usage_ns = SimpleNamespace(
            input_tokens=usage["prompt_tokens"] if usage else 0,
            output_tokens=usage["completion_tokens"] if usage else 0,
        )
        return SimpleNamespace(content=[SimpleNamespace(text=text)], usage=usage_ns)


class CLIClient:
    """OpenAI/Anthropic-compatible client backed by a local CLI agent.

    Exposes both ``.chat.completions.create`` and ``.messages.create`` so it
    works regardless of which provider branch ``llm.py`` takes for the model
    string in use.
    """

    def __init__(
        self,
        backend: str,
        *,
        binary: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        if not is_cli_backend(backend):
            raise ValueError(f"Unknown CLI backend: {backend}")
        self.backend = backend
        self.binary = binary or resolve_binary(backend)
        if not self.binary:
            raise RuntimeError(f"CLI backend '{backend}' binary not found")
        self.model = model
        self.timeout = timeout
        self.chat = _Chat(self)
        self.messages = _Messages(self)

    def _invoke(self, system: str, user: str, want_json: bool) -> Tuple[str, Optional[Dict[str, int]]]:
        prompt = _messages_to_prompt(system, user, want_json)
        if self.backend == "claude":
            return _run_claude(self.binary, prompt, model=self.model, timeout=self.timeout)
        if self.backend == "codex":
            return _run_codex(self.binary, prompt, model=self.model, timeout=self.timeout)
        if self.backend == "agy":
            return _run_agy(self.binary, prompt, model=self.model, timeout=self.timeout)
        raise RuntimeError(f"Unsupported CLI backend: {self.backend}")


def make_backend_client(
    backend: Optional[str],
    *,
    openai_client: Any,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Any:
    """Return a client for ``backend``, falling back to ``openai_client``.

    For non-CLI backends (``openai``/``anthropic``/None) the original client is
    returned unchanged, unless ``base_url`` names an OpenAI-compatible endpoint
    -- then a separate client is built for it. That is how the review step
    reaches a gateway such as Ollama Cloud without moving generation off the
    default backend.

    For CLI backends, a ``CLIClient`` is returned when the binary is available;
    otherwise we log and fall back so the unattended loop never wedges on a
    missing CLI. ``base_url`` is meaningless for a CLI backend and is ignored.
    """
    if not is_cli_backend(backend):
        if not base_url:
            return openai_client
        try:
            from openai import OpenAI

            # Compatible gateways still expect some bearer token; "ollama" is
            # what a local Ollama accepts and is harmless elsewhere.
            # max_retries=0 for the same reason as llm.make_client: retry.py owns
            # retrying, and the SDK default of 2 would compound with it.
            return OpenAI(
                api_key=api_key or "ollama", base_url=base_url, max_retries=0
            )
        except Exception:
            log.warning(
                "OpenAI-compatible endpoint '%s' unavailable; falling back to the default client",
                base_url,
                exc_info=True,
            )
            return openai_client
    try:
        return CLIClient(backend, model=model)
    except Exception:
        log.warning("CLI backend '%s' unavailable; falling back to OpenAI", backend, exc_info=True)
        return openai_client


# --------------------------------------------------------------------------- #
# Agent mode -- CLI edits the working tree, we convert the diff to CodeChanges
# --------------------------------------------------------------------------- #

def _git(repo: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _untracked_files(repo: Path) -> set:
    proc = _git(repo, "ls-files", "--others", "--exclude-standard")
    return {_decode_git_path(line) for line in proc.stdout.splitlines() if line.strip()}


def _build_agent_prompt(task: Any, plan: str, config: Any, model: Optional[str] = None) -> str:
    """Build the CLI agent prompt, bounded to the target model's budget.

    CLI backends take a prompt string rather than a message list, so they do
    not pass through llm.create_completion. The description and plan are
    model-generated and unbounded, so the same ceiling is applied here --
    otherwise the configured generator_backend is a live path with no cap.
    """
    from .llm import model_input_budget, truncate_to_tokens  # local: avoids a cycle

    budget = model_input_budget(model or "")
    description = truncate_to_tokens(
        str(getattr(task, "description", "")), budget // 4, label="task description"
    )
    plan = truncate_to_tokens(plan, budget // 2, label="plan")

    forbidden = ", ".join(getattr(config, "forbidden_modification_paths", ()))
    allowed = ", ".join(getattr(config, "allowed_modification_paths", ()))
    return (
        "You are working inside a git repository. Implement the improvement below "
        "by editing files directly in the working tree.\n\n"
        f"## Task ({getattr(task, 'task_type', '')})\n{description}\n\n"
        f"## Plan\n{plan}\n\n"
        "## Hard constraints (violating any of these causes your work to be discarded)\n"
        f"- Modify at most {getattr(config, 'max_changed_files_per_pr', 3)} files.\n"
        f"- Change at most {getattr(config, 'max_lines_changed_per_pr', 200)} lines in total.\n"
        f"- Only edit files under: {allowed}\n"
        f"- NEVER edit any of: {forbidden}, config.py, git_ops.py\n"
        "- Do NOT run git commit, git push, git add, or alter git history.\n"
        "- Do NOT delete files.\n"
        "- The change must keep the existing test suite passing.\n"
    )


def _snapshot_tracked_dirty(repo: Path) -> Dict[str, str]:
    """Contents of every already-modified tracked file, before the agent runs.

    Without this, a file that was dirty beforehand is indistinguishable from one
    the agent edited: `_collect_changes` compares against HEAD, so it attributed
    the pre-existing edit to the agent. On 2026-08-21 that turned an untouched
    `config/learnings.md` into an agent change and tripped the forbidden-path
    policy, failing a cycle whose actual code changes were fine.
    """
    snapshot: Dict[str, str] = {}
    proc = _git(repo, "status", "--porcelain")
    for status, path in _git_porcelain_changes(proc.stdout):
        if status == "??" or "D" in status:
            continue
        try:
            snapshot[path] = (repo / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return snapshot


def _collect_changes(
    repo: Path,
    untracked_before: set,
    code_change_cls: Any,
    dirty_before: Optional[Dict[str, str]] = None,
) -> List[Any]:
    dirty_before = dirty_before or {}
    proc = _git(repo, "status", "--porcelain")
    changes: List[Any] = []
    for status, path in _git_porcelain_changes(proc.stdout):
        if "D" in status:
            log.info("agent deleted %s -- deletions unsupported in agent mode, skipping", path)
            continue
        # Ignore untracked files that already existed before the agent ran
        # (e.g. local db/wal files); only agent-created ones count.
        if status == "??" and path in untracked_before:
            continue
        full = repo / path
        head = _git(repo, "show", f"HEAD:{path}")
        original = head.stdout if head.returncode == 0 else ""
        try:
            new_content = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if new_content == original:
            continue
        # Already dirty before the agent ran and untouched by it: not its work.
        if path in dirty_before and new_content == dirty_before[path]:
            continue
        changes.append(code_change_cls(
            file_path=path,
            original_content=original,
            new_content=new_content,
            description=f"Agent edit: {path}",
        ))
    return changes


def _reset_worktree(
    repo: Path,
    untracked_before: set,
    dirty_before: Optional[Dict[str, str]] = None,
) -> None:
    """Restore tracked files, preserving pre-existing dirty contents."""
    _git(repo, "reset", "-q")            # unstage anything the agent staged
    _git(repo, "checkout", "--", ".")    # restore modified tracked files
    if dirty_before:
        for path, content in dirty_before.items():
            try:
                (repo / path).write_text(content, encoding="utf-8")
            except OSError:
                pass
    for path in _untracked_files(repo) - untracked_before:
        try:
            (repo / path).unlink()
        except OSError:
            pass


def agent_generate_changes(
    task: Any,
    plan: str,
    repo_root: Any,
    config: Any,
    backend: str,
    *,
    model: Optional[str] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Tuple[Optional[List[Any]], Optional[Dict[str, int]]]:
    """Run a CLI agent to edit the tree, then return (changes, usage).

    The working tree is always reset to its pre-run state before returning, so
    the caller re-applies the resulting ``CodeChange`` list through the normal
    validate/test/revert pipeline (which enforces the safety caps and
    forbidden-path checks on the agent's diff). Returns (None, usage) when the
    agent produces nothing usable.
    """
    from .improvement import CodeChange  # local import avoids a cycle

    binary = resolve_binary(backend)
    if not binary:
        log.warning("agent backend '%s' binary not found", backend)
        return None, None

    repo = Path(repo_root)
    untracked_before = _untracked_files(repo)
    dirty_before = _snapshot_tracked_dirty(repo)
    prompt = _build_agent_prompt(task, plan, config, model)

    try:
        if backend == "claude":
            _text, usage = _run_claude(binary, prompt, model=model, cwd=str(repo), edit=True, timeout=timeout)
        elif backend == "codex":
            _text, usage = _run_codex(binary, prompt, model=model, cwd=str(repo), edit=True, timeout=timeout)
        elif backend == "agy":
            _text, usage = _run_agy(binary, prompt, model=model, cwd=str(repo), edit=True, timeout=timeout)
        else:
            return None, None
    except Exception:
        log.warning("agent run failed for backend '%s'", backend, exc_info=True)
        _reset_worktree(repo, untracked_before, dirty_before)
        return None, None

    changes = _collect_changes(repo, untracked_before, CodeChange, dirty_before)
    _reset_worktree(repo, untracked_before, dirty_before)
    if not changes:
        log.info("agent backend '%s' produced no changes", backend)
        return None, usage
    return changes, usage
