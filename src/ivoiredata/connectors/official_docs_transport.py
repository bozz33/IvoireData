from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..deadline import HardDeadlineExceeded, deadline_remaining_seconds
from ..state_io import atomic_write_json
from . import official_docs as base


DEFAULT_CONNECT_TIMEOUT_SECONDS = 6.1
DEFAULT_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_DEADLINE_RESERVE_SECONDS = 10.0
_MIN_REQUEST_TIMEOUT_SECONDS = 0.25
_DISCOVERY_METHODS = {"DOCS_DISCOVERY_INDEX", "DOCS_CRAWL_DISCOVERY"}


def _iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _env_seconds(name: str, default: float, *, minimum: float) -> float:
    raw = os.getenv(name)
    try:
        value = float(raw) if raw is not None and raw.strip() else float(default)
    except (TypeError, ValueError):
        value = float(default)
    return max(float(minimum), value)


def _configured_timeout() -> tuple[float, float]:
    return (
        _env_seconds(
            "IVOIREDATA_DOCS_CONNECT_TIMEOUT",
            DEFAULT_CONNECT_TIMEOUT_SECONDS,
            minimum=_MIN_REQUEST_TIMEOUT_SECONDS,
        ),
        _env_seconds(
            "IVOIREDATA_DOCS_READ_TIMEOUT",
            DEFAULT_READ_TIMEOUT_SECONDS,
            minimum=_MIN_REQUEST_TIMEOUT_SECONDS,
        ),
    )


def _requested_pair(value: Any) -> tuple[float | None, float | None]:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        out: list[float | None] = []
        for item in value:
            try:
                parsed = float(item) if item is not None else None
            except (TypeError, ValueError):
                parsed = None
            out.append(parsed if parsed is not None and parsed > 0 else None)
        return out[0], out[1]
    try:
        parsed = float(value) if value is not None else None
    except (TypeError, ValueError):
        parsed = None
    if parsed is None or parsed <= 0:
        return None, None
    return parsed, parsed


def _effective_timeout(
    requested: Any,
    *,
    url: str,
    soft_stop: bool,
) -> tuple[float, float]:
    connect, read = _configured_timeout()
    requested_connect, requested_read = _requested_pair(requested)
    if requested_connect is not None:
        connect = min(connect, requested_connect)
    if requested_read is not None:
        read = min(read, requested_read)

    remaining = deadline_remaining_seconds()
    if remaining is not None:
        reserve = _env_seconds(
            "IVOIREDATA_DOCS_DEADLINE_RESERVE",
            DEFAULT_DEADLINE_RESERVE_SECONDS,
            minimum=0.0,
        )
        available = float(remaining) - reserve
        if soft_stop and available <= 0:
            # Existing discovery/crawl loops already treat LimitExceeded as a clean
            # truncated run and break immediately. Reuse that control path instead of
            # inventing a second exception that broad connector handlers could swallow.
            raise base.LimitExceeded(url, 0)
        if available > 0:
            connect = min(connect, max(_MIN_REQUEST_TIMEOUT_SECONDS, available))
            read = min(read, max(_MIN_REQUEST_TIMEOUT_SECONDS, available))
        else:
            connect = min(connect, _MIN_REQUEST_TIMEOUT_SECONDS)
            read = min(read, _MIN_REQUEST_TIMEOUT_SECONDS)

    return (
        max(_MIN_REQUEST_TIMEOUT_SECONDS, float(connect)),
        max(_MIN_REQUEST_TIMEOUT_SECONDS, float(read)),
    )


def _active_path(snapshot_dir: Path | None) -> Path | None:
    if snapshot_dir is None:
        return None
    return Path(snapshot_dir) / "official_docs_active_request.json"


def _write_active(snapshot_dir: Path | None, payload: dict[str, Any]) -> None:
    path = _active_path(snapshot_dir)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, payload)
    except Exception:
        # Request observability must never mask the actual connector/network result.
        pass


class _BoundedSession:
    """Delegate Requests calls while clamping socket inactivity timeouts.

    Requests accepts ``(connect, read)`` timeout tuples. The outer target watchdog is a
    separate wall-clock guard; this proxy deliberately keeps each network wait well
    below that guard and also respects its remaining cooperative budget.
    """

    def __init__(self, session, *, soft_stop: bool):
        self._session = session
        self._soft_stop = bool(soft_stop)
        self.last_timeout: tuple[float, float] | None = None
        self.last_url: str | None = None
        self.last_method: str | None = None

    def _call(self, method: str, url: str, **kwargs):
        timeout = _effective_timeout(
            kwargs.get("timeout"),
            url=str(url),
            soft_stop=self._soft_stop,
        )
        kwargs["timeout"] = timeout
        self.last_timeout = timeout
        self.last_url = str(url)
        self.last_method = method.upper()
        return getattr(self._session, method)(url, **kwargs)

    def get(self, url: str, **kwargs):
        return self._call("get", url, **kwargs)

    def head(self, url: str, **kwargs):
        return self._call("head", url, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._session, name)


_original_fetch = base._fetch
_original_root = base._root
_original_robots_allowed = base._robots_allowed


def _bounded_fetch(session, **kwargs):
    method = str(kwargs.get("method") or "OFFICIAL_DOC_HTTP")
    source_id = str(kwargs.get("source_id") or "")
    url = str(kwargs.get("url") or "")
    snapshot_dir = kwargs.get("snapshot_dir")
    soft_stop = method in _DISCOVERY_METHODS
    proxy = _BoundedSession(session, soft_stop=soft_stop)
    started_mono = time.monotonic()
    started = _iso()
    remaining_before = deadline_remaining_seconds()
    base_state: dict[str, Any] = {
        "status": "RUNNING",
        "source_id": source_id,
        "logical_method": method,
        "url": url,
        "started_at": started,
        "target_deadline_remaining_seconds": (
            round(float(remaining_before), 3) if remaining_before is not None else None
        ),
        "configured_connect_timeout_seconds": _configured_timeout()[0],
        "configured_read_timeout_seconds": _configured_timeout()[1],
        "deadline_reserve_seconds": _env_seconds(
            "IVOIREDATA_DOCS_DEADLINE_RESERVE",
            DEFAULT_DEADLINE_RESERVE_SECONDS,
            minimum=0.0,
        ),
    }
    _write_active(snapshot_dir, base_state)

    try:
        result = _original_fetch(proxy, **kwargs)
    except base.LimitExceeded as exc:
        elapsed = time.monotonic() - started_mono
        soft_deadline = int(getattr(exc, "limit", -1)) == 0 and soft_stop
        _write_active(
            snapshot_dir,
            {
                **base_state,
                "status": "SOFT_DEADLINE" if soft_deadline else "LIMIT_EXCEEDED",
                "finished_at": _iso(),
                "elapsed_seconds": round(elapsed, 3),
                "http_method": proxy.last_method,
                "request_url": proxy.last_url,
                "effective_timeout": list(proxy.last_timeout) if proxy.last_timeout else None,
                "error": str(exc)[:2000],
            },
        )
        raise
    except HardDeadlineExceeded as exc:
        _write_active(
            snapshot_dir,
            {
                **base_state,
                "status": "HARD_DEADLINE",
                "finished_at": _iso(),
                "elapsed_seconds": round(time.monotonic() - started_mono, 3),
                "http_method": proxy.last_method,
                "request_url": proxy.last_url,
                "effective_timeout": list(proxy.last_timeout) if proxy.last_timeout else None,
                "error": str(exc)[:2000],
            },
        )
        raise
    except BaseException as exc:
        _write_active(
            snapshot_dir,
            {
                **base_state,
                "status": "ERROR",
                "finished_at": _iso(),
                "elapsed_seconds": round(time.monotonic() - started_mono, 3),
                "http_method": proxy.last_method,
                "request_url": proxy.last_url,
                "effective_timeout": list(proxy.last_timeout) if proxy.last_timeout else None,
                "error_type": type(exc).__name__,
                "error": str(exc)[:2000],
            },
        )
        raise

    response = result[0] if isinstance(result, tuple) and result else None
    _write_active(
        snapshot_dir,
        {
            **base_state,
            "status": "FINISHED",
            "finished_at": _iso(),
            "elapsed_seconds": round(time.monotonic() - started_mono, 3),
            "http_method": proxy.last_method,
            "request_url": proxy.last_url,
            "effective_timeout": list(proxy.last_timeout) if proxy.last_timeout else None,
            "http_status": getattr(response, "status_code", None),
        },
    )
    return result


def _bounded_root(session, url: str, ua: str) -> str:
    return _original_root(_BoundedSession(session, soft_stop=False), url, ua)


def _bounded_robots_allowed(session, url: str, user_agent: str, cache) -> bool:
    return _original_robots_allowed(
        _BoundedSession(session, soft_stop=True),
        url,
        user_agent,
        cache,
    )


# Patch only the shared HTTP transport helpers. Discovery scope, conditional validators,
# SHA-256 replay, dlt state and two-phase target migration remain owned by the existing
# connector layers.
base._fetch = _bounded_fetch
base._root = _bounded_root
base._robots_allowed = _bounded_robots_allowed
