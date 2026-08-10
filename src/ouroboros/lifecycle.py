"""Process-wide shutdown signal.

Lives in its own module so anything that waits -- the main loop's sleeps, the
retry backoff in llm and moltbook -- can observe the same event without those
modules importing each other.
"""

import threading

shutdown_event = threading.Event()


def is_shutting_down() -> bool:
    """Return True once a graceful shutdown has been requested."""
    return shutdown_event.is_set()


def request_shutdown() -> None:
    shutdown_event.set()


def reset() -> None:
    """Clear the flag. For tests; the runner sets it once and exits."""
    shutdown_event.clear()
