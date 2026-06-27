"""Tests for the local CLI agent backends (claude/codex)."""

import json
import subprocess
from pathlib import Path

import pytest

from ouroboros import backends


# --------------------------------------------------------------------------- #
# Binary resolution + backend predicates
# --------------------------------------------------------------------------- #

def test_is_cli_backend():
    assert backends.is_cli_backend("claude")
    assert backends.is_cli_backend("codex")
    assert backends.is_cli_backend("agy")
    assert not backends.is_cli_backend("openai")
    assert not backends.is_cli_backend(None)


def test_parse_agy_output():
    assert backends.parse_agy_output("  OK done\n") == "OK done"
    assert backends.parse_agy_output('```json\n{"a":1}\n```') == '```json\n{"a":1}\n```'


def test_cliclient_agy_invoke(monkeypatch):
    monkeypatch.setattr(backends, "resolve_binary", lambda name: "/usr/bin/" + name)
    captured = {}

    def fake_run(binary, prompt, *, model=None, timeout=600):
        captured["binary"] = binary
        return "agy-reply", None

    monkeypatch.setattr(backends, "_run_agy", fake_run)
    client = backends.CLIClient("agy")
    resp = client.chat.completions.create(messages=[{"role": "user", "content": "hi"}])
    assert resp.choices[0].message.content == "agy-reply"
    assert resp.usage is None  # agy does not report tokens
    assert captured["binary"] == "/usr/bin/agy"


def test_resolve_binary_falls_back_to_known_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(backends.shutil, "which", lambda name: None)
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(backends, "_EXTRA_BIN_DIRS", (str(tmp_path),))
    assert backends.resolve_binary("claude") == str(fake)


def test_resolve_binary_missing(monkeypatch):
    monkeypatch.setattr(backends.shutil, "which", lambda name: None)
    monkeypatch.setattr(backends, "_EXTRA_BIN_DIRS", ())
    assert backends.resolve_binary("claude") is None


# --------------------------------------------------------------------------- #
# Output parsing
# --------------------------------------------------------------------------- #

def test_parse_claude_output_success():
    stdout = json.dumps({
        "is_error": False,
        "result": "hello",
        "usage": {"input_tokens": 12, "output_tokens": 3},
    })
    text, usage = backends.parse_claude_output(stdout)
    assert text == "hello"
    assert usage == {"prompt_tokens": 12, "completion_tokens": 3}


def test_parse_claude_output_error_raises():
    stdout = json.dumps({"is_error": True, "result": "boom"})
    with pytest.raises(RuntimeError):
        backends.parse_claude_output(stdout)


def test_parse_codex_output_extracts_final_answer():
    stdout = (
        "OpenAI Codex v0.1\n--------\nuser\nhi\ncodex\nOK done\ntokens used\n7,284\n"
    )
    assert backends.parse_codex_output(stdout) == "OK done"


def test_parse_codex_output_fallback_when_no_marker():
    assert backends.parse_codex_output("just text") == "just text"


# --------------------------------------------------------------------------- #
# CLIClient duck-typing (no real subprocess)
# --------------------------------------------------------------------------- #

@pytest.fixture
def fake_claude(monkeypatch):
    monkeypatch.setattr(backends, "resolve_binary", lambda name: "/usr/bin/" + name)

    def fake_invoke(self, system, user, want_json):
        return f"S={system}|U={user}|J={want_json}", {"prompt_tokens": 5, "completion_tokens": 2}

    monkeypatch.setattr(backends.CLIClient, "_invoke", fake_invoke)
    return backends.CLIClient("claude")


def test_cliclient_chat_completions(fake_claude):
    resp = fake_claude.chat.completions.create(
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "usr"},
        ],
        response_format={"type": "json_object"},
    )
    msg = resp.choices[0].message
    assert "S=sys" in msg.content and "U=usr" in msg.content and "J=True" in msg.content
    assert msg.tool_calls is None
    assert resp.usage.prompt_tokens == 5
    assert resp.usage.completion_tokens == 2


def test_cliclient_messages_interface(fake_claude):
    resp = fake_claude.messages.create(
        system="sys",
        messages=[{"role": "user", "content": "usr"}],
    )
    assert "S=sys" in resp.content[0].text
    assert resp.usage.input_tokens == 5
    assert resp.usage.output_tokens == 2


def test_cliclient_unknown_backend_rejected(monkeypatch):
    with pytest.raises(ValueError):
        backends.CLIClient("not-a-backend")


# --------------------------------------------------------------------------- #
# make_backend_client routing + fallback
# --------------------------------------------------------------------------- #

def test_make_backend_client_passthrough_for_openai():
    sentinel = object()
    assert backends.make_backend_client("openai", openai_client=sentinel) is sentinel


def test_make_backend_client_falls_back_when_missing(monkeypatch):
    monkeypatch.setattr(backends, "resolve_binary", lambda name: None)
    sentinel = object()
    assert backends.make_backend_client("claude", openai_client=sentinel) is sentinel


def test_make_backend_client_returns_cliclient(monkeypatch):
    monkeypatch.setattr(backends, "resolve_binary", lambda name: "/usr/bin/claude")
    sentinel = object()
    client = backends.make_backend_client("claude", openai_client=sentinel)
    assert isinstance(client, backends.CLIClient)


# --------------------------------------------------------------------------- #
# Agent mode -- real temp git repo, mocked CLI run
# --------------------------------------------------------------------------- #

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    src = repo / "src"
    src.mkdir()
    (src / "mod.py").write_text("x = 1\n")
    # A pre-existing untracked file that must survive the reset.
    (repo / "untracked.txt").write_text("keep me\n")
    _git(repo, "add", "src/mod.py")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


class _Task:
    task_type = "fix_bug"
    description = "bump x"


class _Cfg:
    max_changed_files_per_pr = 3
    max_lines_changed_per_pr = 200
    allowed_modification_paths = ("src/",)
    forbidden_modification_paths = ("config.py",)


def test_agent_generate_changes_captures_diff_and_resets(monkeypatch, git_repo):
    def fake_run_claude(binary, prompt, *, model=None, cwd=None, edit=False, timeout=600):
        # Simulate the agent editing a tracked file and creating a new one.
        (Path(cwd) / "src" / "mod.py").write_text("x = 2\n")
        (Path(cwd) / "src" / "new.py").write_text("y = 9\n")
        return "done", {"prompt_tokens": 10, "completion_tokens": 4}

    monkeypatch.setattr(backends, "resolve_binary", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(backends, "_run_claude", fake_run_claude)

    changes, usage = backends.agent_generate_changes(
        _Task(), "the plan", git_repo, _Cfg(), "claude",
    )

    assert usage == {"prompt_tokens": 10, "completion_tokens": 4}
    by_path = {c.file_path: c for c in changes}
    assert by_path["src/mod.py"].original_content == "x = 1\n"
    assert by_path["src/mod.py"].new_content == "x = 2\n"
    assert by_path["src/new.py"].original_content == ""
    assert by_path["src/new.py"].new_content == "y = 9\n"

    # Tree is reset: tracked file restored, agent-created file removed,
    # pre-existing untracked file preserved.
    assert (git_repo / "src" / "mod.py").read_text() == "x = 1\n"
    assert not (git_repo / "src" / "new.py").exists()
    assert (git_repo / "untracked.txt").read_text() == "keep me\n"


def test_agent_generate_changes_no_changes_returns_none(monkeypatch, git_repo):
    def fake_run_claude(binary, prompt, *, model=None, cwd=None, edit=False, timeout=600):
        return "did nothing", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(backends, "resolve_binary", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(backends, "_run_claude", fake_run_claude)

    changes, usage = backends.agent_generate_changes(
        _Task(), "plan", git_repo, _Cfg(), "claude",
    )
    assert changes is None
    assert usage == {"prompt_tokens": 1, "completion_tokens": 1}


def test_agent_generate_changes_resets_on_crash(monkeypatch, git_repo):
    def fake_run_claude(binary, prompt, *, model=None, cwd=None, edit=False, timeout=600):
        (Path(cwd) / "src" / "mod.py").write_text("corrupted\n")
        raise RuntimeError("agent died mid-edit")

    monkeypatch.setattr(backends, "resolve_binary", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(backends, "_run_claude", fake_run_claude)

    changes, usage = backends.agent_generate_changes(
        _Task(), "plan", git_repo, _Cfg(), "claude",
    )
    assert changes is None
    # Partial edit was rolled back.
    assert (git_repo / "src" / "mod.py").read_text() == "x = 1\n"


def test_agent_generate_changes_missing_binary(monkeypatch, git_repo):
    monkeypatch.setattr(backends, "resolve_binary", lambda name: None)
    changes, usage = backends.agent_generate_changes(
        _Task(), "plan", git_repo, _Cfg(), "claude",
    )
    assert changes is None and usage is None
