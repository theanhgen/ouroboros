"""Tests for retry with exponential backoff and jitter."""

import ssl
import urllib.error
import urllib.request

import pytest

from ouroboros.retry import (
    DEFAULT_MAX_ATTEMPTS,
    is_idempotent_request,
    backoff_delay,
    is_retryable,
    retry_with_backoff,
)


class _Recorder:
    """Stand-in for time.sleep that records the delays instead of waiting."""

    def __init__(self):
        self.delays = []

    def __call__(self, delay):
        self.delays.append(delay)


# -- is_retryable ------------------------------------------------------------

@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("timed out"),
        ConnectionError("connection reset"),
        ConnectionResetError("reset"),
        urllib.error.URLError("no route to host"),
    ],
)
def test_is_retryable_transport_failures(exc):
    assert is_retryable(exc) is True


@pytest.mark.parametrize(
    "code", [408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524]
)
def test_is_retryable_transient_status_codes(code):
    exc = urllib.error.HTTPError("u", code, "msg", {}, None)
    assert is_retryable(exc) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 422, 507, 508])
def test_is_not_retryable_client_errors(code):
    """Retrying a bad request, a bad key, or a conflict just burns quota.

    409 is a deterministic statement about state; 507/508 do not clear on
    their own.
    """
    exc = urllib.error.HTTPError("u", code, "msg", {}, None)
    assert is_retryable(exc) is False


@pytest.mark.parametrize(
    "exc", [ValueError("nope"), KeyError("k"), RuntimeError("boom")]
)
def test_is_not_retryable_ordinary_exceptions(exc):
    assert is_retryable(exc) is False


def test_is_retryable_openai_rate_limit():
    openai = pytest.importorskip("openai")
    exc = openai.RateLimitError.__new__(openai.RateLimitError)
    assert is_retryable(exc) is True


def test_is_retryable_openai_bad_request_is_not():
    openai = pytest.importorskip("openai")
    exc = openai.BadRequestError.__new__(openai.BadRequestError)
    assert is_retryable(exc) is False


def test_is_retryable_urlerror_wrapping_dns_failure():
    """A name that will not resolve stays retryable, and the comment says so.

    gaierror is not proof of a typo -- "temporary failure in name resolution"
    arrives the same way, and the openai backend already retries this as
    APIConnectionError. Flipping it to permanent would cost a cycle every time
    the Pi's resolver blinks.
    """
    # socket is on SafetyConfig's forbidden-import list and a policy test scans
    # this suite, so reach the real class through urllib instead.
    gaierror = urllib.request.socket.gaierror
    exc = urllib.error.URLError(gaierror(-3, "Temporary failure in name resolution"))
    assert is_retryable(exc) is True


def test_is_not_retryable_urlerror_wrapping_ssl_error():
    """The one reason the unwrap exists for: a certificate never gets better."""
    exc = urllib.error.URLError(ssl.SSLError("certificate verify failed"))
    assert is_retryable(exc) is False


def test_is_retryable_uses_status_code_attribute():
    class Weird(Exception):
        status_code = 503

    assert is_retryable(Weird()) is True

    class WeirdClient(Exception):
        status_code = 400

    assert is_retryable(WeirdClient()) is False


# -- backoff_delay -----------------------------------------------------------

def test_backoff_delay_ceiling_grows_exponentially():
    ceilings = [
        backoff_delay(n, base_delay=1.0, max_delay=30.0, rng=lambda lo, hi: hi)
        for n in range(1, 6)
    ]
    assert ceilings == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_backoff_delay_respects_max_delay():
    ceiling = backoff_delay(20, base_delay=1.0, max_delay=30.0, rng=lambda lo, hi: hi)
    assert ceiling == 30.0


def test_backoff_delay_uses_full_jitter():
    """Uniform over [0, ceiling] -- not ceiling plus a nudge."""
    assert backoff_delay(3, rng=lambda lo, hi: lo) == 0.0
    seen = {round(backoff_delay(4), 6) for _ in range(200)}
    assert len(seen) > 50, "delay should be jittered, not constant"
    assert all(0.0 <= d <= 8.0 for d in seen)


# -- retry_with_backoff ------------------------------------------------------

def test_returns_immediately_on_success():
    sleep = _Recorder()
    calls = []

    @retry_with_backoff(sleep=sleep)
    def fn():
        calls.append(1)
        return "ok"

    assert fn() == "ok"
    assert len(calls) == 1
    assert sleep.delays == []


def test_retries_then_succeeds():
    sleep = _Recorder()
    calls = []

    @retry_with_backoff(sleep=sleep)
    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("transient")
        return "ok"

    assert fn() == "ok"
    assert len(calls) == 3
    assert len(sleep.delays) == 2


def test_gives_up_after_max_attempts_and_reraises():
    """The final failure propagates; callers already handle errors."""
    sleep = _Recorder()
    calls = []

    @retry_with_backoff(max_attempts=3, sleep=sleep)
    def fn():
        calls.append(1)
        raise TimeoutError("always down")

    with pytest.raises(TimeoutError, match="always down"):
        fn()

    assert len(calls) == 3
    assert len(sleep.delays) == 2  # no sleep after the last attempt


def test_does_not_retry_non_retryable():
    sleep = _Recorder()
    calls = []

    @retry_with_backoff(sleep=sleep)
    def fn():
        calls.append(1)
        raise ValueError("bad request")

    with pytest.raises(ValueError):
        fn()

    assert len(calls) == 1
    assert sleep.delays == []


def test_default_attempt_count():
    sleep = _Recorder()
    calls = []

    @retry_with_backoff(sleep=sleep)
    def fn():
        calls.append(1)
        raise TimeoutError()

    with pytest.raises(TimeoutError):
        fn()
    assert len(calls) == DEFAULT_MAX_ATTEMPTS


def test_preserves_function_metadata_and_arguments():
    @retry_with_backoff(sleep=_Recorder())
    def add(a, b, *, c=0):
        """Adds things."""
        return a + b + c

    assert add.__name__ == "add"
    assert add.__doc__ == "Adds things."
    assert add(1, 2, c=3) == 6


def test_custom_retryable_predicate():
    sleep = _Recorder()
    calls = []

    @retry_with_backoff(max_attempts=3, sleep=sleep, retryable=lambda e: True)
    def fn():
        calls.append(1)
        raise ValueError("normally not retryable")

    with pytest.raises(ValueError):
        fn()
    assert len(calls) == 3


# -- wiring: the retry actually applies to the real call paths ---------------

def test_llm_completions_retry_transient_failures(monkeypatch):
    """Every llm.py completion goes through one retrying chokepoint."""
    from ouroboros import llm, retry

    monkeypatch.setattr(retry.time, "sleep", lambda _d: None)
    attempts = []

    class FlakyCompletions:
        def create(self, **kwargs):
            attempts.append(kwargs)
            if len(attempts) < 3:
                raise TimeoutError("transient")
            return _completion_response("recovered")

    client = _client_with(FlakyCompletions())
    content, _usage = llm.chat_completion(client, "sys", "user", model="gpt-test")

    assert content == "recovered"
    assert len(attempts) == 3


def test_llm_completions_do_not_retry_bad_requests(monkeypatch):
    from ouroboros import llm, retry

    monkeypatch.setattr(retry.time, "sleep", lambda _d: None)
    attempts = []

    class BadCompletions:
        def create(self, **kwargs):
            attempts.append(kwargs)
            raise ValueError("invalid model")

    # chat_completion swallows the failure and returns ("", None) as before.
    content, usage = llm.chat_completion(
        _client_with(BadCompletions()), "sys", "user", model="gpt-test"
    )

    assert (content, usage) == ("", None)
    assert len(attempts) == 1


def test_moltbook_request_retries_transient_http(monkeypatch):
    from ouroboros import moltbook, retry

    monkeypatch.setattr(retry.time, "sleep", lambda _d: None)
    attempts = []

    class FakeResponse:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def flaky_urlopen(req, timeout=None):
        attempts.append(req)
        if len(attempts) < 3:
            raise urllib.error.HTTPError("u", 503, "unavailable", {}, None)
        return FakeResponse()

    monkeypatch.setattr(moltbook.urllib.request, "urlopen", flaky_urlopen)

    assert moltbook._request("GET", "/x", "key") == {"ok": True}
    assert len(attempts) == 3


def test_moltbook_request_does_not_retry_auth_failure(monkeypatch):
    from ouroboros import moltbook, retry

    monkeypatch.setattr(retry.time, "sleep", lambda _d: None)
    attempts = []

    def unauthorized(req, timeout=None):
        attempts.append(req)
        raise urllib.error.HTTPError("u", 401, "unauthorized", {}, None)

    monkeypatch.setattr(moltbook.urllib.request, "urlopen", unauthorized)

    with pytest.raises(moltbook.MoltbookError):
        moltbook._request("GET", "/x", "key")
    assert len(attempts) == 1


def test_moltbook_request_still_wraps_the_final_error(monkeypatch):
    """Exhausting retries must still surface as MoltbookError, as before."""
    from ouroboros import moltbook, retry

    monkeypatch.setattr(retry.time, "sleep", lambda _d: None)

    def always_down(req, timeout=None):
        raise urllib.error.HTTPError("u", 503, "unavailable", {}, None)

    monkeypatch.setattr(moltbook.urllib.request, "urlopen", always_down)

    with pytest.raises(moltbook.MoltbookError, match="Request failed"):
        moltbook._request("GET", "/x", "key")


# -- helpers -----------------------------------------------------------------

def _completion_response(text):
    class Message:
        content = text
        tool_calls = None

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]
        usage = None

    return Response()


def _client_with(completions):
    class Chat:
        pass

    chat = Chat()
    chat.completions = completions

    class Client:
        pass

    client = Client()
    client.chat = chat
    return client


def test_sleep_is_resolved_at_call_time_not_decoration_time(monkeypatch):
    """Otherwise a decorated call site can never be tested without real waits."""
    from ouroboros import retry

    @retry_with_backoff(max_attempts=2)
    def fn():
        raise TimeoutError("transient")

    recorded = []
    monkeypatch.setattr(retry.time, "sleep", lambda d: recorded.append(d))

    with pytest.raises(TimeoutError):
        fn()

    assert len(recorded) == 1, "patching retry.time.sleep must take effect"


# -- idempotency: replaying a write can duplicate it -------------------------

@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "PUT", "DELETE"])
def test_idempotent_methods_are_replayable(method):
    req = urllib.request.Request("https://x/y", method=method)
    assert is_idempotent_request(req) is True


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_write_methods_are_not_replayable(method):
    req = urllib.request.Request("https://x/y", data=b"{}", method=method)
    assert is_idempotent_request(req) is False


def test_unknown_request_shape_is_treated_as_unsafe():
    """A missed retry costs a cycle; a wrong one costs a duplicate post."""
    assert is_idempotent_request(object()) is False


def test_moltbook_does_not_retry_post(monkeypatch):
    """create_post must never be replayed -- the server may have committed it."""
    from ouroboros import moltbook, retry

    monkeypatch.setattr(retry.time, "sleep", lambda _d: None)
    attempts = []

    def flaky(req, timeout=None):
        attempts.append(req.get_method())
        raise urllib.error.HTTPError("u", 503, "unavailable", {}, None)

    monkeypatch.setattr(moltbook.urllib.request, "urlopen", flaky)

    with pytest.raises(moltbook.MoltbookError):
        moltbook.create_post("key", "general", "title", "body")

    assert attempts == ["POST"], "a POST must be sent exactly once"


def test_moltbook_does_not_retry_comment(monkeypatch):
    from ouroboros import moltbook, retry

    monkeypatch.setattr(retry.time, "sleep", lambda _d: None)
    attempts = []

    def flaky(req, timeout=None):
        attempts.append(req.get_method())
        raise TimeoutError("response lost after the server committed")

    monkeypatch.setattr(moltbook.urllib.request, "urlopen", flaky)

    with pytest.raises(moltbook.MoltbookError):
        moltbook.create_comment("key", "post-1", "hello")

    assert attempts == ["POST"]


def test_moltbook_still_retries_reads(monkeypatch):
    from ouroboros import moltbook, retry

    monkeypatch.setattr(retry.time, "sleep", lambda _d: None)
    attempts = []

    class FakeResponse:
        def read(self):
            return b'{"posts": []}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def flaky(req, timeout=None):
        attempts.append(req.get_method())
        if len(attempts) < 3:
            raise urllib.error.HTTPError("u", 429, "slow down", {}, None)
        return FakeResponse()

    monkeypatch.setattr(moltbook.urllib.request, "urlopen", flaky)

    assert moltbook.get_feed("key") == {"posts": []}
    assert attempts == ["GET", "GET", "GET"]


# -- shutdown ----------------------------------------------------------------

def test_retry_stops_when_cancelled():
    """An outage must not hold the process past SIGTERM."""
    sleep = _Recorder()
    calls = []
    cancelled = []

    @retry_with_backoff(max_attempts=4, sleep=sleep, cancelled=lambda: bool(cancelled))
    def fn():
        calls.append(1)
        cancelled.append(True)  # shutdown requested during attempt 1
        raise TimeoutError("service down")

    with pytest.raises(TimeoutError):
        fn()

    assert len(calls) == 1, "must not start another attempt after shutdown"
    assert sleep.delays == [], "must not sleep after shutdown"


def test_retry_stops_when_cancelled_during_backoff():
    calls = []
    flag = []

    def sleep_then_cancel(_delay):
        flag.append(True)

    @retry_with_backoff(max_attempts=4, sleep=sleep_then_cancel, cancelled=lambda: bool(flag))
    def fn():
        calls.append(1)
        raise TimeoutError("service down")

    with pytest.raises(TimeoutError):
        fn()

    assert len(calls) == 1


def test_moltbook_retry_is_wired_to_the_shutdown_event(monkeypatch):
    from ouroboros import moltbook, retry

    monkeypatch.setattr(retry.time, "sleep", lambda _d: None)
    attempts = []

    def flaky(req, timeout=None):
        attempts.append(1)
        moltbook._shutdown_event.set()
        raise urllib.error.HTTPError("u", 503, "unavailable", {}, None)

    monkeypatch.setattr(moltbook.urllib.request, "urlopen", flaky)
    try:
        with pytest.raises(moltbook.MoltbookError):
            moltbook.get_feed("key")
        assert len(attempts) == 1
    finally:
        moltbook._shutdown_event.clear()


# -- no completion call may bypass the chokepoint ----------------------------

def test_no_module_calls_chat_completions_create_directly():
    """Every completion must route through llm.create_completion for retries."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "ouroboros"
    offenders = []
    for path in sorted(src.rglob("*.py")):
        if path.name in {"llm.py", "backends.py"}:
            continue  # llm defines the entrypoint; backends duck-types the API
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "create"
                and isinstance(f.value, ast.Attribute)
                and f.value.attr == "completions"
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == [], (
        "these bypass llm.create_completion and so get no retries: "
        + ", ".join(offenders)
    )


# -- one retry owner, one budget, one shutdown -------------------------------

def test_reviewer_endpoint_client_disables_sdk_retries(monkeypatch):
    """Otherwise the SDK's 2 compound with our 4 -- up to 12 transmissions."""
    from ouroboros import backends

    built = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)
    backends.make_backend_client(
        "openai", openai_client=object(), base_url="https://ollama.com/v1"
    )
    assert built["max_retries"] == 0


def test_llm_retries_stop_on_shutdown(monkeypatch):
    """SIGTERM during an LLM outage must not cost three more attempts."""
    from ouroboros import lifecycle, llm, retry

    monkeypatch.setattr(retry.time, "sleep", lambda _d: None)
    attempts = []

    class Completions:
        def create(self, **kwargs):
            attempts.append(kwargs)
            lifecycle.request_shutdown()
            raise TimeoutError("service down")

    try:
        content, usage = llm.chat_completion(
            _client_with(Completions()), "sys", "user", model="gpt-test"
        )
        assert (content, usage) == ("", None)
        assert len(attempts) == 1
    finally:
        lifecycle.reset()


def test_identify_does_not_double_the_retry_budget(monkeypatch):
    """A transport outage costs four attempts total, not eight."""
    from ouroboros import llm, retry

    monkeypatch.setattr(retry.time, "sleep", lambda _d: None)
    attempts = []

    class Completions:
        def create(self, **kwargs):
            attempts.append(kwargs)
            raise TimeoutError("service down")

    result, err = llm.identify_improvements(
        _client_with(Completions()), "summary", "tests", "history", model="gpt-test"
    )

    assert result is None
    assert err is not None
    assert len(attempts) == retry.DEFAULT_MAX_ATTEMPTS


def test_identify_still_falls_back_for_non_transport_failures(monkeypatch):
    """A capability or shape problem is exactly what the fallback is for."""
    from ouroboros import llm, retry

    monkeypatch.setattr(retry.time, "sleep", lambda _d: None)
    attempts = []

    class Completions:
        def create(self, **kwargs):
            attempts.append(kwargs)
            if "tools" in kwargs:
                raise ValueError("this model does not support tools")
            return _completion_response('{"task_type": "fix_bug"}')

    result, err = llm.identify_improvements(
        _client_with(Completions()), "summary", "tests", "history", model="gpt-test"
    )

    assert err is None
    assert result["task_type"] == "fix_bug"
    assert len(attempts) == 2  # one tool-call attempt, one plain-JSON fallback
