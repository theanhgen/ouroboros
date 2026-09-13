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
