"""identify_model / plan_model reach the CLI clients for those roles.

Without them identify and plan ran on whatever the CLI defaulted to -- for
codex, the model in ~/.codex/config.toml -- with no way to pin a cheaper one.
"""

import subprocess
from unittest.mock import patch

import pytest

from ouroboros import backends, improvement
from ouroboros.config import SafetyConfig
from ouroboros.improvement import ImprovementResult, ImprovementTask
from ouroboros.improvement_runner import run_scheduled_self_improvement
from ouroboros.model_defaults import DEFAULT_OPENAI_MODEL
from ouroboros.moltbook import RunnerConfig


class _Stop(Exception):
    pass


def test_cycle_passes_role_models_to_identify_and_plan_clients(monkeypatch, tmp_path):
    calls = []

    def fake_make_backend_client(backend, *, openai_client, model=None, base_url=None, api_key=None):
        calls.append((backend, model))
        if len(calls) == 2:
            raise _Stop
        return object()

    monkeypatch.setattr(improvement, "get_repo_root", lambda: tmp_path)
    monkeypatch.setattr(improvement, "ToolRunner", lambda root: None)
    monkeypatch.setattr(backends, "make_backend_client", fake_make_backend_client)

    config = SafetyConfig(
        identify_backend="codex", identify_model="gpt-5.6-luna",
        plan_backend="codex", plan_model="gpt-5.6-luna",
    )
    with pytest.raises(_Stop):
        improvement._run_improvement_cycle({}, object(), {}, config)

    assert calls == [("codex", "gpt-5.6-luna"), ("codex", "gpt-5.6-luna")]


def test_role_models_default_to_unset():
    config = SafetyConfig()
    assert config.identify_model is None
    assert config.plan_model is None


def test_run_codex_pins_the_given_model(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(backends.subprocess, "run", fake_run)
    text, _ = backends._run_codex("/bin/codex", "prompt", model="gpt-5.6-luna")

    assert seen["cmd"] == ["/bin/codex", "exec", "-c", 'model="gpt-5.6-luna"', "prompt"]
    assert text == '{"ok": true}'


@patch("ouroboros.improvement_runner.time.time", return_value=1_700_000_000)
@patch("ouroboros.improvement_runner.save_scheduler_state")
@patch("ouroboros.improvement_runner.load_scheduler_state", return_value={"consecutive_failures": 0, "next_due_ts": None})
@patch("ouroboros.improvement_runner._load_feed_context_state", return_value={})
@patch("ouroboros.improvement_runner.llm.make_client")
@patch("ouroboros.improvement_runner.llm.load_openai_key", return_value="key")
@patch("ouroboros.improvement_runner.run_improvement_cycle")
@patch("ouroboros.improvement_runner.git_ops.has_open_improvement_prs", return_value=False)
@patch("ouroboros.improvement_runner.git_ops.is_clean", return_value=True)
@patch("ouroboros.improvement_runner.check_pr_outcomes")
@patch("ouroboros.improvement_runner.get_repo_root", return_value="/tmp/repo")
@patch("ouroboros.improvement_runner.load_runner_config")
def test_scheduled_runner_carries_role_models_into_safety_config(
    mock_cfg, _repo_root, _check_prs, _is_clean, _open_prs, mock_run_cycle,
    _load_key, _make_client, _feed_state, _load_state, _save_state, _time,
):
    mock_cfg.return_value = RunnerConfig(
        enable_self_improvement=True,
        improvement_model=DEFAULT_OPENAI_MODEL,
        identify_model="gpt-5.6-luna",
        plan_model="gpt-5.6-luna",
    )
    task = ImprovementTask("abc", "fix_bug", "x", ["src/ouroboros/cli.py"], "Bug")
    mock_run_cycle.return_value = ImprovementResult(task=task, status="success")

    run_scheduled_self_improvement(force=True)

    safety = mock_run_cycle.call_args.args[2]
    assert safety.identify_model == "gpt-5.6-luna"
    assert safety.plan_model == "gpt-5.6-luna"


def test_run_codex_through_gateway_keeps_the_key_out_of_env_and_argv(monkeypatch, tmp_path):
    """codex_base_url moves codex off the ChatGPT account onto the gateway,
    with the key in a private CODEX_HOME that is removed afterwards."""
    import os

    seen = {}

    def fake_run(cmd, **kwargs):
        env = kwargs["env"]
        home = env["CODEX_HOME"]
        seen.update(cmd=cmd, env=env, home=home,
                    config=open(os.path.join(home, "config.toml")).read(),
                    mode=os.stat(home).st_mode & 0o777)
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    monkeypatch.setattr(backends.subprocess, "run", fake_run)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_API_KEY", "sk-or-test")
    backends._run_codex(
        "/bin/codex", "prompt", model="cohere/north-mini-code:free",
        edit=True, base_url="https://openrouter.ai/api/v1",
    )

    cmd = seen["cmd"]
    assert cmd[:4] == ["/bin/codex", "exec", "--sandbox", "workspace-write"]
    # Any model id is passed through, not only gpt-* ones.
    assert 'model="cohere/north-mini-code:free"' in cmd
    assert cmd[-1] == "prompt"
    assert 'base_url = "https://openrouter.ai/api/v1"' in seen["config"]
    assert 'experimental_bearer_token = "sk-or-test"' in seen["config"]
    assert seen["mode"] == 0o700
    # Neither argv nor the environment the agent's commands inherit holds it.
    assert not any("sk-or-test" in part for part in cmd)
    assert "sk-or-test" not in seen["env"].values()
    assert not os.path.exists(seen["home"])


def test_run_codex_without_gateway_keeps_the_default_env(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    monkeypatch.setattr(backends.subprocess, "run", fake_run)
    backends._run_codex("/bin/codex", "prompt", model="cohere/x:free")
    assert seen["env"] is None


def test_agent_generate_passes_codex_base_url(monkeypatch, tmp_path):
    seen = {}

    def fake_run_codex(binary, prompt, **kwargs):
        seen.update(kwargs)
        return "", None

    monkeypatch.setattr(backends, "resolve_binary", lambda b: "/bin/codex")
    monkeypatch.setattr(backends, "_run_codex", fake_run_codex)
    monkeypatch.setattr(backends, "_untracked_files", lambda repo: set())
    monkeypatch.setattr(backends, "_snapshot_tracked_dirty", lambda repo: {})
    monkeypatch.setattr(backends, "_build_agent_prompt", lambda *a: "p")
    monkeypatch.setattr(backends, "_reset_worktree", lambda *a: None)
    monkeypatch.setattr(backends, "_git", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))

    config = SafetyConfig(codex_base_url="https://openrouter.ai/api/v1")
    backends.agent_generate_changes(
        ImprovementTask(task_id="t", task_type="fix_bug", description="d", target_files=[], evidence="e"),
        "plan", tmp_path, config, "codex", model="m:free",
    )
    assert seen["base_url"] == "https://openrouter.ai/api/v1"


def test_run_codex_passes_reasoning_effort_through_gateway(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="done", stderr="")

    monkeypatch.setattr(backends.subprocess, "run", fake_run)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_API_KEY", "sk-or-test")
    backends._run_codex(
        "/bin/codex", "prompt", model="a:free", edit=True,
        base_url="https://openrouter.ai/api/v1", reasoning_effort="high",
    )
    assert 'model_reasoning_effort="high"' in seen["cmd"]


def test_codex_falls_back_to_the_next_gateway_model(monkeypatch, tmp_path):
    """A free model that 429s upstream must not end the generate step."""
    tried, resets = [], []

    def fake_run_codex(binary, prompt, *, model=None, base_url=None, reasoning_effort=None, **kw):
        tried.append((model, reasoning_effort))
        if model == "a:free":
            raise backends.CLIBackendError("codex exited 1: 429")
        return "done", None

    monkeypatch.setattr(backends, "_run_codex", fake_run_codex)
    monkeypatch.setattr(backends, "_reset_worktree", lambda *a: resets.append(a))
    cfg = SafetyConfig(
        codex_base_url="https://openrouter.ai/api/v1",
        codex_fallback_models=("a:free", "b:free", "c:free"),
        codex_reasoning_effort="high",
    )
    text, _ = backends._run_codex_with_fallbacks(
        "/bin/codex", "p", "a:free", tmp_path, cfg, 60, set(), {}
    )
    assert text == "done"
    assert tried == [("a:free", "high"), ("b:free", "high")]
    assert len(resets) == 1


def test_codex_without_gateway_does_not_fall_back(monkeypatch, tmp_path):
    def fake_run_codex(*a, **kw):
        raise backends.CLIBackendError("codex exited 1")

    monkeypatch.setattr(backends, "_run_codex", fake_run_codex)
    cfg = SafetyConfig(codex_fallback_models=("b:free",))
    with pytest.raises(backends.CLIBackendError):
        backends._run_codex_with_fallbacks("/bin/codex", "p", None, tmp_path, cfg, 60, set(), {})
