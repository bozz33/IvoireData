from __future__ import annotations

from typing import Any, Iterator

import requests
from requests.adapters import HTTPAdapter

from .http_client import BudgetedSession, _CURRENT_HTTP_RUN, _build_retry


_ORIGINAL_SESSION_REQUEST = requests.Session.request
_INSTALLED = False


def _configure_session(session: requests.Session, context) -> None:
    marker = getattr(session, "_ivoiredata_http_run_id", None)
    if marker == context.run_id:
        return
    adapter = HTTPAdapter(
        max_retries=_build_retry(context.policy),
        pool_connections=context.policy.pool_connections,
        pool_maxsize=context.policy.pool_maxsize,
        pool_block=True,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.setdefault("User-Agent", context.user_agent)
    session.headers.setdefault("Accept-Encoding", "gzip, deflate")
    setattr(session, "_ivoiredata_http_run_id", context.run_id)


def _instrumented_request(self: requests.Session, method: str, url: str, **kwargs):
    # BudgetedSession already implements the same accounting itself. This branch avoids
    # double-counting when its super().request resolves to our patched Session.request.
    if isinstance(self, BudgetedSession):
        return _ORIGINAL_SESSION_REQUEST(self, method, url, **kwargs)

    context = _CURRENT_HTTP_RUN.get()
    if context is None:
        return _ORIGINAL_SESSION_REQUEST(self, method, url, **kwargs)

    _configure_session(self, context)
    context.before_request(url)
    # Persist the logical request before entering potentially long/blocking network I/O.
    # A crash/kill during the request therefore leaves a useful last checkpoint.
    context.checkpoint("RUNNING")
    kwargs.setdefault(
        "timeout",
        (context.policy.connect_timeout_seconds, context.policy.read_timeout_seconds),
    )
    stream_requested = bool(kwargs.get("stream", False))
    try:
        response = _ORIGINAL_SESSION_REQUEST(self, method, url, **kwargs)
    except BaseException as exc:
        context.after_exception(exc)
        raise

    context.after_response(response)
    if stream_requested:
        original_iter_content = response.iter_content

        def iter_content(chunk_size=1, decode_unicode=False) -> Iterator[Any]:
            try:
                for chunk in original_iter_content(
                    chunk_size=chunk_size, decode_unicode=decode_unicode
                ):
                    if chunk:
                        size = (
                            len(chunk.encode(response.encoding or "utf-8", "replace"))
                            if isinstance(chunk, str)
                            else len(chunk)
                        )
                        context.consume_bytes(size)
                    yield chunk
            finally:
                context.checkpoint("RUNNING")

        response.iter_content = iter_content  # type: ignore[method-assign]
    else:
        context.consume_bytes(len(response.content or b""))
    return response


def install_requests_runtime() -> None:
    """Install one process-wide, context-gated Requests shim.

    The shim is inert outside an IvoireData HTTP run context. Inside a source sync it
    supplies the shared adapter/retry policy, default timeouts, per-host pacing, byte
    accounting and run budgets to existing connectors without source-specific rewrites.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    requests.Session.request = _instrumented_request  # type: ignore[method-assign]
    _INSTALLED = True
