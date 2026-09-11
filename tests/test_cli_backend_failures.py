"""A CLI backend that cannot answer must say why, and must not be asked twice.

On 2026-09-11 agy's quota was exhausted for hours. agy knew within a second but
retried silently until its print timeout, printed nothing, and each identify
cycle reported "Failed to parse LLM response: Expecting value: line 1 column 1
(char 0)" -- after two full timeouts, because the plain-JSON fallback ran the
same CLI again.
"""

import os
import stat
import subprocess
import sys
import time

import pytest

from ouroboros import backends, llm, retry

QUOTA_LINE = (
    "I0911 17:05:53.703564     365 run.go:387] Run: attempt 1 failed (RESOURCE_EXHAUSTED "
    "(code 429): Individual quota reached. Please upgrade your subscription to increase your "
    "limits. Resets in {reset}.), retrying in 4s\n"
)


def _fake_agy(tmp_path, *, reset=None, stdout="", sleep=0.0, exit_code=0):
    """An executable that behaves like agy: writes its log, maybe waits, maybe answers."""
    script = tmp_path / "agy"
    log_line = QUOTA_LINE.format(reset=reset) if reset else "I0911 run.go:1] Print mode: starting\n"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "args = sys.argv[1:]\n"
        "with open(args[args.index('--log-file') + 1], 'a') as fh:\n"
        f"    fh.write({log_line!r})\n"
        f"time.sleep({sleep})\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setattr(backends, "_QUOTA_POLL_SECONDS", 0.05)


# -- _run_agy ------------------------------------------------------------------

def test_a_quota_that_outlasts_the_deadline_fails_fast(tmp_path, fast_poll):
    agy = _fake_agy(tmp_path, reset="54m30s", sleep=30)
    started = time.monotonic()
    with pytest.raises(backends.CLIBackendError, match="agy quota exhausted: Individual quota reached"):
        backends._run_agy(agy, "hi", timeout=20)
    assert time.monotonic() - started < 10  # not the 20s deadline, let alone agy's 30s


def test_a_quota_that_resets_before_the_deadline_is_waited_out(tmp_path, fast_poll):
    agy = _fake_agy(tmp_path, reset="1s", stdout="OK", sleep=0.5)
    assert backends._run_agy(agy, "hi", timeout=20) == ("OK", None)


def test_nothing_printed_after_a_quota_error_names_the_quota(tmp_path, fast_poll):
    # An expired --print-timeout looks like this: exit 0, empty stdout.
    agy = _fake_agy(tmp_path, reset="1s")
    with pytest.raises(backends.CLIBackendError, match="quota exhausted"):
        backends._run_agy(agy, "hi", timeout=20)


def test_a_non_zero_exit_is_a_backend_error(tmp_path, fast_poll):
    agy = _fake_agy(tmp_path, exit_code=1)
    with pytest.raises(backends.CLIBackendError, match="agy exited 1"):
        backends._run_agy(agy, "hi", timeout=20)


def test_a_successful_call_leaves_no_log_behind(tmp_path, fast_poll, monkeypatch):
    made = []
    real_mkstemp = backends.tempfile.mkstemp

    def spy(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        made.append(path)
        return fd, path

    monkeypatch.setattr(backends.tempfile, "mkstemp", spy)
    agy = _fake_agy(tmp_path, stdout="reply")
    assert backends._run_agy(agy, "hi", timeout=20) == ("reply", None)
    assert made and not os.path.exists(made[0])


# -- CLIClient -----------------------------------------------------------------

def test_an_empty_reply_is_a_backend_error_not_a_parse_error(monkeypatch):
    monkeypatch.setattr(backends, "resolve_binary", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(backends, "_run_agy", lambda binary, prompt, *, model=None, timeout=600: ("", None))
    with pytest.raises(backends.CLIBackendError, match="printed nothing"):
        backends.CLIClient("agy").chat.completions.create(messages=[{"role": "user", "content": "hi"}])


def test_a_cli_timeout_is_a_backend_error(monkeypatch):
    monkeypatch.setattr(backends, "resolve_binary", lambda name: "/usr/bin/" + name)

    def hang(binary, prompt, *, model=None, timeout=600):
        raise subprocess.TimeoutExpired(binary, timeout)

    monkeypatch.setattr(backends, "_run_agy", hang)
    with pytest.raises(backends.CLIBackendError, match="timed out"):
        backends.CLIClient("agy").chat.completions.create(messages=[{"role": "user", "content": "hi"}])


# -- identify_improvements -----------------------------------------------------

def _client(create):
    completions = type("Completions", (), {"create": staticmethod(create)})()
    chat = type("Chat", (), {})()
    chat.completions = completions
    client = type("Client", (), {})()
    client.chat = chat
    return client


def test_identify_does_not_rerun_a_backend_that_could_not_answer(monkeypatch):
    monkeypatch.setattr(retry.time, "sleep", lambda _d: None)
    attempts = []

    def create(**kwargs):
        attempts.append(kwargs)
        raise backends.CLIBackendError("agy quota exhausted: Individual quota reached.")

    result, err = llm.identify_improvements(_client(create), "summary", "tests", "history", model="gpt-test")

    assert result is None
    assert err == "agy quota exhausted: Individual quota reached."
    assert len(attempts) == 1


def test_identify_reports_why_the_fallback_call_failed(monkeypatch):
    monkeypatch.setattr(retry.time, "sleep", lambda _d: None)

    def create(**kwargs):
        if "tools" in kwargs:
            raise ValueError("this model does not support tools")
        raise backends.CLIBackendError("agy exited 1: boom")

    result, err = llm.identify_improvements(_client(create), "summary", "tests", "history", model="gpt-test")

    assert result is None
    assert "agy exited 1: boom" in err
    assert "Expecting value" not in err
