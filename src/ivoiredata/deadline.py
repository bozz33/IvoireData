from __future__ import annotations

import os
import signal
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


class HardDeadlineExceeded(BaseException):
    """Wall-clock deadline that must bypass broad ``except Exception`` handlers.

    Dynamic documentation sync wraps many third-party/network layers which commonly
    convert normal ``Exception`` subclasses into retryable results. The watchdog must
    be able to escape those layers, return control to the scheduler, and persist a
    clean RETRY state instead of being swallowed inside a connector.
    """


_CURRENT_DEADLINE_MONOTONIC: ContextVar[float | None] = ContextVar(
    "ivoiredata_current_deadline_monotonic",
    default=None,
)


def deadline_remaining_seconds() -> float | None:
    """Return the cooperative wall-clock budget for the current operation.

    The hard POSIX signal remains the final safety net. This helper lets inner network
    layers choose request timeouts that are materially shorter than the target watchdog
    and stop starting new discovery work before that watchdog has to fire.
    """

    deadline = _CURRENT_DEADLINE_MONOTONIC.get()
    if deadline is None:
        return None
    return max(0.0, float(deadline) - time.monotonic())


def hard_deadline_supported() -> bool:
    return bool(
        os.name == "posix"
        and threading.current_thread() is threading.main_thread()
        and hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
        and hasattr(signal, "ITIMER_REAL")
    )


@contextmanager
def hard_deadline(seconds: float, *, label: str) -> Iterator[None]:
    """Bound an operation by wall clock and expose the remaining cooperative budget.

    On POSIX/main-thread runs this also arms SIGALRM, so locks, native code and third-
    party layers that do not cooperate are still forcibly interrupted. On unsupported
    platforms the context cannot provide that signal-based hard stop, but the deadline
    budget is still exposed to cooperative HTTP layers.
    """

    timeout = max(0.0, float(seconds))
    if timeout <= 0:
        yield
        return

    now = time.monotonic()
    outer_deadline = _CURRENT_DEADLINE_MONOTONIC.get()
    requested_deadline = now + timeout
    effective_deadline = (
        min(float(outer_deadline), requested_deadline)
        if outer_deadline is not None
        else requested_deadline
    )
    token = _CURRENT_DEADLINE_MONOTONIC.set(effective_deadline)

    if not hard_deadline_supported():
        try:
            yield
        finally:
            _CURRENT_DEADLINE_MONOTONIC.reset(token)
        return

    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    armed_timeout = max(0.001, effective_deadline - time.monotonic())

    def _raise_deadline(_signum, _frame):
        raise HardDeadlineExceeded(
            f"hard deadline exceeded after {timeout:.3f}s: {label}"
        )

    signal.signal(signal.SIGALRM, _raise_deadline)
    signal.setitimer(signal.ITIMER_REAL, armed_timeout)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        _CURRENT_DEADLINE_MONOTONIC.reset(token)
        # Preserve an outer timer if one existed. The remaining duration cannot be
        # reconstructed perfectly after nesting, but restoring it is safer than
        # silently deleting the caller's watchdog.
        if old_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, old_timer[0], old_timer[1])
