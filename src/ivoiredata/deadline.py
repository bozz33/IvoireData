from __future__ import annotations

import os
import signal
import threading
from contextlib import contextmanager
from typing import Iterator


class HardDeadlineExceeded(BaseException):
    """Wall-clock deadline that must bypass broad ``except Exception`` handlers.

    Dynamic documentation sync wraps many third-party/network layers which commonly
    convert normal ``Exception`` subclasses into retryable results.  The watchdog must
    be able to escape those layers, return control to the scheduler, and persist a
    clean RETRY state instead of being swallowed inside a connector.
    """



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
    """Interrupt the current POSIX main-thread operation after ``seconds``.

    This is deliberately a *wall-clock* guard rather than an HTTP timeout.  It also
    bounds waits that happen before/after networking, notably source locks and dlt
    pipeline stalls.  Docker production runs on POSIX in the main thread, where
    SIGALRM can break a blocking wait.  Non-POSIX direct use still keeps the shorter
    source-lock timeout and ordinary HTTP timeouts but cannot offer this signal-based
    hard stop.
    """

    timeout = max(0.0, float(seconds))
    if timeout <= 0 or not hard_deadline_supported():
        yield
        return

    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)

    def _raise_deadline(_signum, _frame):
        raise HardDeadlineExceeded(f"hard deadline exceeded after {timeout:.3f}s: {label}")

    signal.signal(signal.SIGALRM, _raise_deadline)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        # Preserve an outer timer if one existed.  The remaining duration cannot be
        # reconstructed perfectly after nesting, but restoring it is safer than
        # silently deleting the caller's watchdog.
        if old_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, old_timer[0], old_timer[1])
