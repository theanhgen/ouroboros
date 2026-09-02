"""Retry with exponential backoff and jitter for external API calls.

The agent runs unattended on a Raspberry Pi, so a rate limit or a dropped
connection should cost a short wait rather than a skipped cycle.

Two things this module is careful about:

* Only transient failures are retried. A 400 or a 401 fails identically on
  every attempt, so retrying one delays the error and burns quota.
* Retrying is only safe for idempotent work. Replaying a POST that the server
  already committed but whose response was lost creates a duplicate, so
  callers opt in per request rather than getting retries by default.
"""

import functools
import http.client
import logging
import random
import ssl
import time
from typing import Any, Callable, Optional, Tuple, Type

log = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 1.0
DEFAULT_MAX_DELAY = 30.0

# HTTP statuses worth another attempt: rate limiting, request timeout, and
# server-side faults including the Cloudflare 52x range.
#
# 409 is deliberately absent -- a conflict is a deterministic statement about
# state, and replaying the same request reproduces it. 507 and 508 are absent
# for the same reason: out of storage and loop detected do not clear on their
# own.
RETRYABLE_STATUS_CODES = frozenset(
    {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
)

# Mid-stream failures: the connection was accepted, then died. urllib surfaces
# these without wrapping them in URLError, and none subclass ConnectionError.
RETRYABLE_HTTP_EXCEPTIONS: Tuple[Type[BaseException], ...] = (
    http.client.IncompleteRead,
    http.client.BadStatusLine,      # RemoteDisconnected subclasses this
    http.client.LineTooLong,
    http.client.ResponseNotReady,
)

# HTTP methods safe to replay. Everything else may have taken effect before the
# response was lost.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"})


def _openai_retryable_types() -> Tuple[Type[BaseException], ...]:
    """Return the openai exception classes that represent transient faults.

    Imported lazily and defensively: the SDK is an optional dependency at
    runtime for CLI-backend deployments, and its exception surface has moved
    between major versions.
    """
    try:
        import openai
    except Exception:  # pragma: no cover - openai always present in tests
        return ()

    names = (
        "RateLimitError",
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
    )
    return tuple(
        cls
        for cls in (getattr(openai, name, None) for name in names)
        if isinstance(cls, type) and issubclass(cls, BaseException)
    )


def is_retryable(exc: BaseException) -> bool:
    """Return True if exc looks transient and another attempt may succeed."""
    # A TLS failure is a configuration or trust problem, not a blip. Checked
    # first because URLError wraps it and would otherwise let it through.
    if isinstance(exc, ssl.SSLError) and not isinstance(exc, ssl.SSLWantReadError):
        return False

    if isinstance(exc, RETRYABLE_HTTP_EXCEPTIONS):
        return True

    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True

    retryable_types = _openai_retryable_types()
    if retryable_types and isinstance(exc, retryable_types):
        return True

    # urllib raises HTTPError (a subclass of URLError) carrying a .code.
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code in RETRYABLE_STATUS_CODES

    # openai APIStatusError and similar expose the status this way instead.
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in RETRYABLE_STATUS_CODES

    try:
        from urllib.error import URLError

        if isinstance(exc, URLError):
            # Plain URLError means no response at all. Unwrap the reason so a
            # bad certificate, which fails identically every time, is not
            # retried.
            #
            # A name that does not resolve stays retryable, deliberately.
            # gaierror is also how "temporary failure in name resolution"
            # arrives, and on a Pi the resolver is the likeliest cause; the
            # openai backend already retries the same failure as
            # APIConnectionError, so the urllib path agrees with it. A typo'd
            # host costs one capped backoff before it surfaces, while a
            # resolver blip would otherwise cost a whole cycle.
            reason = getattr(exc, "reason", None)
            if isinstance(reason, ssl.SSLError):
                return False
            return True
    except Exception:  # pragma: no cover - stdlib always importable
        pass

    return False


def is_idempotent_request(req: Any) -> bool:
    """Return True if req may be safely replayed.

    Accepts a urllib Request, or anything exposing get_method()/method.
    Unknown shapes are treated as unsafe: the cost of a missed retry is a
    skipped cycle, the cost of a wrong one is a duplicate public post.
    """
    method = None
    getter = getattr(req, "get_method", None)
    if callable(getter):
        try:
            method = getter()
        except Exception:
            method = None
    if method is None:
        method = getattr(req, "method", None)
    if not isinstance(method, str):
        return False
    return method.upper() in IDEMPOTENT_METHODS


def backoff_delay(
    attempt: int,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    rng: Optional[Callable[[float, float], float]] = None,
) -> float:
    """Return the delay before ``attempt`` (1-based), using full jitter.

    Full jitter -- uniform over [0, capped exponential] -- rather than
    exponential plus a small random term. Several agents retrying a shared
    rate limit in lockstep is the failure this avoids.
    """
    uniform = rng or random.uniform
    ceiling = min(max_delay, base_delay * (2 ** max(0, attempt - 1)))
    return uniform(0.0, ceiling)


def retry_with_backoff(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    retryable: Callable[[BaseException], bool] = is_retryable,
    sleep: Optional[Callable[[float], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> Callable:
    """Retry the wrapped callable on transient failures.

    Non-retryable exceptions propagate immediately, and the last attempt's
    exception propagates rather than being swallowed -- callers already handle
    failure, and hiding it would turn an outage into silently missing data.

    ``cancelled`` is polled before each wait and before each further attempt.
    Without it a service outage would hold the process in backoff across four
    attempts, delaying SIGTERM by up to the sum of the timeouts and waits and
    risking writes issued after shutdown was requested.

    ``sleep`` defaults to ``time.sleep`` resolved at call time, not at
    decoration time. Binding it as a default argument would capture the
    function when the module is imported, so a decorated call site could never
    be tested without waiting out the real backoff.
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            pause = sleep if sleep is not None else time.sleep
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    if attempt >= max_attempts or not retryable(exc):
                        raise
                    if cancelled is not None and cancelled():
                        log.info(
                            "%s failed and shutdown was requested -- not retrying",
                            getattr(fn, "__name__", "call"),
                        )
                        raise
                    delay = backoff_delay(attempt, base_delay, max_delay)
                    log.warning(
                        "%s failed (attempt %d/%d): %s -- retrying in %.1fs",
                        getattr(fn, "__name__", "call"),
                        attempt,
                        max_attempts,
                        exc,
                        delay,
                    )
                    pause(delay)
                    if cancelled is not None and cancelled():
                        log.info(
                            "shutdown requested during backoff -- abandoning %s",
                            getattr(fn, "__name__", "call"),
                        )
                        raise
            raise AssertionError("unreachable")  # pragma: no cover

        return wrapper

    return decorator
