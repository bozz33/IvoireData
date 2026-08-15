from __future__ import annotations

import contextvars
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from .state_io import atomic_write_json


class HttpBudgetExceeded(RuntimeError):
    """Raised when a source exceeds an explicit HTTP run budget."""


@dataclass(frozen=True)
class HttpPolicy:
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 180.0
    retries: int = 4
    backoff_factor: float = 0.5
    backoff_jitter: float = 0.25
    backoff_max_seconds: float = 30.0
    retry_after_max_seconds: int = 300
    pool_connections: int = 16
    pool_maxsize: int = 16
    min_interval_seconds: float = 0.0
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504)


@dataclass(frozen=True)
class HttpBudget:
    max_requests: int | None = 10_000
    max_bytes: int | None = 5_000_000_000
    max_seconds: float | None = 21_600.0
    max_failures: int | None = 50


@dataclass
class HttpMetrics:
    logical_requests: int = 0
    network_attempts: int = 0
    retries: int = 0
    responses_304: int = 0
    bytes_downloaded: int = 0
    failures: int = 0
    rate_limit_wait_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    budget_exceeded: bool = False
    budget_reason: str | None = None
    status_counts: dict[str, int] = field(default_factory=dict)
    host_requests: dict[str, int] = field(default_factory=dict)


_HOST_LOCK = threading.Lock()
_HOST_NEXT_ALLOWED: dict[str, float] = {}
_CURRENT_HTTP_RUN: contextvars.ContextVar["HttpRunContext | None"] = contextvars.ContextVar(
    "ivoiredata_http_run", default=None
)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return float(default)
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return int(default)
    return int(raw)


def _optional_number(value: Any, *, integer: bool = False):
    if value is None or value == "":
        return None
    parsed = int(value) if integer else float(value)
    return None if parsed <= 0 else parsed


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def policy_from_options(options: Mapping[str, Any] | None = None) -> HttpPolicy:
    cfg = _mapping(_mapping(options).get("http_policy"))
    return HttpPolicy(
        connect_timeout_seconds=float(
            cfg.get("connect_timeout_seconds", _env_float("IVOIREDATA_HTTP_CONNECT_TIMEOUT", 10.0))
        ),
        read_timeout_seconds=float(
            cfg.get("read_timeout_seconds", _env_float("IVOIREDATA_HTTP_READ_TIMEOUT", 180.0))
        ),
        retries=max(0, int(cfg.get("retries", _env_int("IVOIREDATA_HTTP_RETRIES", 4)))),
        backoff_factor=max(
            0.0, float(cfg.get("backoff_factor", _env_float("IVOIREDATA_HTTP_BACKOFF_FACTOR", 0.5)))
        ),
        backoff_jitter=max(
            0.0, float(cfg.get("backoff_jitter", _env_float("IVOIREDATA_HTTP_BACKOFF_JITTER", 0.25)))
        ),
        backoff_max_seconds=max(
            0.0, float(cfg.get("backoff_max_seconds", _env_float("IVOIREDATA_HTTP_BACKOFF_MAX", 30.0)))
        ),
        retry_after_max_seconds=max(
            1, int(cfg.get("retry_after_max_seconds", _env_int("IVOIREDATA_HTTP_RETRY_AFTER_MAX", 300)))
        ),
        pool_connections=max(
            1, int(cfg.get("pool_connections", _env_int("IVOIREDATA_HTTP_POOL_CONNECTIONS", 16)))
        ),
        pool_maxsize=max(1, int(cfg.get("pool_maxsize", _env_int("IVOIREDATA_HTTP_POOL_MAXSIZE", 16)))),
        min_interval_seconds=max(
            0.0, float(cfg.get("min_interval_seconds", _env_float("IVOIREDATA_HTTP_MIN_INTERVAL", 0.0)))
        ),
        retry_statuses=tuple(int(x) for x in cfg.get("retry_statuses", (429, 500, 502, 503, 504))),
    )


def budget_from_options(options: Mapping[str, Any] | None = None) -> HttpBudget:
    cfg = _mapping(_mapping(options).get("http_budget"))
    max_requests = cfg.get("max_requests", os.getenv("IVOIREDATA_HTTP_MAX_REQUESTS", "10000"))
    max_bytes = cfg.get("max_bytes", os.getenv("IVOIREDATA_HTTP_MAX_BYTES", "5000000000"))
    max_seconds = cfg.get("max_seconds", os.getenv("IVOIREDATA_HTTP_MAX_SECONDS", "21600"))
    max_failures = cfg.get("max_failures", os.getenv("IVOIREDATA_HTTP_MAX_FAILURES", "50"))
    return HttpBudget(
        max_requests=_optional_number(max_requests, integer=True),
        max_bytes=_optional_number(max_bytes, integer=True),
        max_seconds=_optional_number(max_seconds),
        max_failures=_optional_number(max_failures, integer=True),
    )


def _build_retry(policy: HttpPolicy) -> Retry:
    kwargs: dict[str, Any] = {
        "total": policy.retries,
        "connect": policy.retries,
        "read": policy.retries,
        "status": policy.retries,
        "allowed_methods": frozenset({"GET", "HEAD", "OPTIONS"}),
        "status_forcelist": policy.retry_statuses,
        "backoff_factor": policy.backoff_factor,
        "backoff_max": policy.backoff_max_seconds,
        "respect_retry_after_header": True,
        "raise_on_status": False,
        "backoff_jitter": policy.backoff_jitter,
        "retry_after_max": policy.retry_after_max_seconds,
    }
    # Requests may be paired with different urllib3 versions. Keep the strongest
    # supported retry policy without making an optional Retry keyword a startup failure.
    for optional in ("retry_after_max", "backoff_jitter"):
        try:
            return Retry(**kwargs)
        except TypeError:
            kwargs.pop(optional, None)
    return Retry(**kwargs)


def _reserve_host_slot(host: str, interval: float) -> float:
    if not host or interval <= 0:
        return 0.0
    now = time.monotonic()
    with _HOST_LOCK:
        target = max(now, _HOST_NEXT_ALLOWED.get(host, now))
        _HOST_NEXT_ALLOWED[host] = target + interval
    return max(0.0, target - now)


class HttpRunContext:
    def __init__(
        self,
        *,
        source_id: str,
        run_id: str,
        state_dir: Path | None,
        user_agent: str,
        policy: HttpPolicy,
        budget: HttpBudget,
    ) -> None:
        self.source_id = source_id
        self.run_id = run_id
        self.user_agent = user_agent
        self.policy = policy
        self.budget = budget
        self.metrics = HttpMetrics()
        self.started_monotonic = time.monotonic()
        self.checkpoint_path = (
            Path(state_dir) / "http_runs" / f"{run_id}.json" if state_dir is not None else None
        )
        # _trip() may checkpoint while a budget counter is already protected, so this
        # must be re-entrant rather than a plain Lock.
        self._lock = threading.RLock()
        self._token = None
        self._bytes_since_checkpoint = 0
        self.checkpoint("RUNNING")

    def __enter__(self) -> "HttpRunContext":
        self._token = _CURRENT_HTTP_RUN.set(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        with self._lock:
            self.metrics.elapsed_seconds = self._elapsed()
        self.checkpoint(
            "BUDGET_EXCEEDED" if self.metrics.budget_exceeded else ("ERROR" if exc else "FINISHED")
        )
        if self._token is not None:
            _CURRENT_HTTP_RUN.reset(self._token)

    def _elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started_monotonic)

    def _trip(self, reason: str) -> None:
        with self._lock:
            self.metrics.budget_exceeded = True
            self.metrics.budget_reason = reason
            self.metrics.elapsed_seconds = self._elapsed()
        self.checkpoint("BUDGET_EXCEEDED")
        raise HttpBudgetExceeded(reason)

    def check_time(self) -> None:
        elapsed = self._elapsed()
        if self.budget.max_seconds is not None and elapsed > self.budget.max_seconds:
            self._trip(f"HTTP time budget exceeded: {elapsed:.3f}s > {self.budget.max_seconds:.3f}s")

    def before_request(self, url: str) -> None:
        self.check_time()
        with self._lock:
            next_count = self.metrics.logical_requests + 1
            if self.budget.max_requests is not None and next_count > self.budget.max_requests:
                self._trip(f"HTTP request budget exceeded: {next_count} > {self.budget.max_requests}")
            self.metrics.logical_requests = next_count
            host = (urlparse(url).hostname or "").casefold()
            self.metrics.host_requests[host] = self.metrics.host_requests.get(host, 0) + 1
        wait = _reserve_host_slot((urlparse(url).hostname or "").casefold(), self.policy.min_interval_seconds)
        if wait > 0:
            time.sleep(wait)
            with self._lock:
                self.metrics.rate_limit_wait_seconds += wait
        self.check_time()

    def after_response(self, response: requests.Response) -> None:
        retries_obj = getattr(getattr(response, "raw", None), "retries", None)
        retry_count = len(tuple(getattr(retries_obj, "history", ()) or ()))
        status = int(response.status_code)
        with self._lock:
            self.metrics.network_attempts += 1 + retry_count
            self.metrics.retries += retry_count
            key = str(status)
            self.metrics.status_counts[key] = self.metrics.status_counts.get(key, 0) + 1
            if status == 304:
                self.metrics.responses_304 += 1
            # 404 is deliberately not a run-budget failure: connectors such as Data.gouv
            # use authoritative 404s to classify upstream tombstones/ghost catalogue rows.
            if status == 429 or status >= 500:
                self.metrics.failures += 1
                failures = self.metrics.failures
            else:
                failures = self.metrics.failures
            self.metrics.elapsed_seconds = self._elapsed()
        self.checkpoint("RUNNING")
        if self.budget.max_failures is not None and failures > self.budget.max_failures:
            self._trip(f"HTTP failure budget exceeded: {failures} > {self.budget.max_failures}")
        self.check_time()

    def after_exception(self, exc: BaseException) -> None:
        with self._lock:
            self.metrics.network_attempts += 1
            self.metrics.failures += 1
            self.metrics.elapsed_seconds = self._elapsed()
            failures = self.metrics.failures
        self.checkpoint("RUNNING")
        if self.budget.max_failures is not None and failures > self.budget.max_failures:
            self._trip(
                f"HTTP failure budget exceeded after {type(exc).__name__}: "
                f"{failures} > {self.budget.max_failures}"
            )
        self.check_time()

    def consume_bytes(self, amount: int) -> None:
        if amount <= 0:
            return
        with self._lock:
            next_bytes = self.metrics.bytes_downloaded + int(amount)
            if self.budget.max_bytes is not None and next_bytes > self.budget.max_bytes:
                self._trip(f"HTTP byte budget exceeded: {next_bytes} > {self.budget.max_bytes}")
            self.metrics.bytes_downloaded = next_bytes
            self._bytes_since_checkpoint += int(amount)
            checkpoint_due = self._bytes_since_checkpoint >= 8 * 1024 * 1024
            if checkpoint_due:
                self._bytes_since_checkpoint = 0
        self.check_time()
        if checkpoint_due:
            self.checkpoint("RUNNING")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self.metrics.elapsed_seconds = self._elapsed()
            return {
                "source_id": self.source_id,
                "run_id": self.run_id,
                "policy": asdict(self.policy),
                "budget": asdict(self.budget),
                **asdict(self.metrics),
            }

    def checkpoint(self, status: str) -> None:
        if self.checkpoint_path is None:
            return
        payload = self.snapshot()
        payload["status"] = status
        payload["checkpointed_at_epoch"] = time.time()
        atomic_write_json(self.checkpoint_path, payload)


class BudgetedSession(requests.Session):
    def __init__(self, context: HttpRunContext, *, verify_ssl: bool = True) -> None:
        super().__init__()
        self.context = context
        self.verify = verify_ssl
        self.headers.update({
            "User-Agent": context.user_agent,
            "Accept-Encoding": "gzip, deflate",
        })
        adapter = HTTPAdapter(
            max_retries=_build_retry(context.policy),
            pool_connections=context.policy.pool_connections,
            pool_maxsize=context.policy.pool_maxsize,
            pool_block=True,
        )
        self.mount("http://", adapter)
        self.mount("https://", adapter)

    def request(self, method: str, url: str, **kwargs):
        self.context.before_request(url)
        kwargs.setdefault(
            "timeout",
            (self.context.policy.connect_timeout_seconds, self.context.policy.read_timeout_seconds),
        )
        stream_requested = bool(kwargs.get("stream", False))
        try:
            response = super().request(method, url, **kwargs)
        except BaseException as exc:
            self.context.after_exception(exc)
            raise
        self.context.after_response(response)

        if stream_requested:
            original_iter_content = response.iter_content
            context = self.context

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
            self.context.consume_bytes(len(response.content or b""))
        return response


def new_session(user_agent: str, *, verify_ssl: bool = True) -> BudgetedSession:
    context = _CURRENT_HTTP_RUN.get()
    if context is None:
        # Audits/helpers outside Engine.sync still receive pooling, retries and timeouts,
        # but intentionally do not consume a source budget or create checkpoint files.
        context = HttpRunContext(
            source_id="standalone",
            run_id=f"standalone-{os.getpid()}-{time.time_ns()}",
            state_dir=None,
            user_agent=user_agent,
            policy=policy_from_options({}),
            budget=HttpBudget(max_requests=None, max_bytes=None, max_seconds=None, max_failures=None),
        )
    return BudgetedSession(context, verify_ssl=verify_ssl)


def current_http_run() -> HttpRunContext | None:
    return _CURRENT_HTTP_RUN.get()


def http_run_context(
    *,
    source_id: str,
    run_id: str,
    state_dir: Path,
    user_agent: str,
    options: Mapping[str, Any] | None = None,
) -> HttpRunContext:
    return HttpRunContext(
        source_id=source_id,
        run_id=run_id,
        state_dir=state_dir,
        user_agent=user_agent,
        policy=policy_from_options(options),
        budget=budget_from_options(options),
    )
