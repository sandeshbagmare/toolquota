"""Client-side per-tool rate limits an agent can act on.

`backoff-headers` reads what a server said about its limits, after it said it. By then the
agent has already made twenty calls and the twenty-first is waiting. A tool that allows
ten requests a minute should be limited *before* the first of them.

    >>> from toolquota import Quota, ToolQuota
    >>> quota = ToolQuota(default=Quota(rate=1, burst=2))
    >>> quota.try_acquire("search"), quota.try_acquire("search")
    (True, True)
    >>> quota.try_acquire("search")
    False
    >>> round(quota.wait_time("search"), 1)
    1.0

Token buckets, one per tool, with an injected clock so tests do not sleep.
Standard library only, Python 3.9+.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

__all__ = ["Quota", "QuotaExceeded", "ToolQuota"]

__version__ = "0.1.0"


@dataclass(frozen=True)
class Quota:
    """A token bucket: ``rate`` calls per second, up to ``burst`` at once.

    ``max_calls`` is an optional absolute cap for the whole run, which is what a paid or
    slow tool usually needs ("search may run 20 times, ever").
    """

    rate: float
    burst: int = 1
    max_calls: int | None = None

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError("rate must be greater than zero")
        if self.burst < 1:
            raise ValueError("burst must be at least 1")
        if self.max_calls is not None and self.max_calls < 1:
            raise ValueError("max_calls must be at least 1")


class QuotaExceeded(RuntimeError):
    """Raised by :meth:`ToolQuota.acquire` when a call is not allowed now."""

    def __init__(self, tool: str, wait_time: float, *, permanent: bool = False) -> None:
        self.tool = tool
        self.wait_time = wait_time
        self.permanent = permanent
        if permanent:
            message = f"{tool!r} has used its whole quota for this run"
        else:
            message = f"{tool!r} is rate limited; retry in {wait_time:.2f}s"
        super().__init__(message)


@dataclass
class _Bucket:
    tokens: float
    updated: float
    calls: int = 0


@dataclass
class ToolQuota:
    """Per-tool token buckets.

    ``default`` applies to tools with no entry in ``quotas``. ``clock`` defaults to
    :func:`time.monotonic`; pass a function returning a float to test without sleeping.
    """

    default: Quota = field(default_factory=lambda: Quota(rate=1.0, burst=1))
    quotas: Mapping[str, Quota] = field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic
    _buckets: dict[str, _Bucket] = field(default_factory=dict, repr=False)

    def _quota(self, tool: str) -> Quota:
        return self.quotas.get(tool, self.default)

    def _bucket(self, tool: str) -> _Bucket:
        quota = self._quota(tool)
        now = self.clock()
        bucket = self._buckets.get(tool)
        if bucket is None:
            bucket = _Bucket(tokens=float(quota.burst), updated=now)
            self._buckets[tool] = bucket
        else:
            elapsed = max(now - bucket.updated, 0.0)
            if elapsed:
                bucket.tokens = min(quota.burst, bucket.tokens + elapsed * quota.rate)
                bucket.updated = now
        return bucket

    # -- asking ----------------------------------------------------------

    def remaining(self, tool: str) -> float:
        """How many calls the bucket holds right now (may be fractional)."""
        return round(self._bucket(tool).tokens, 6)

    def wait_time(self, tool: str, *, count: int = 1) -> float:
        """Seconds to wait before ``count`` calls would be allowed; ``0.0`` if now."""
        quota = self._quota(tool)
        bucket = self._bucket(tool)
        if quota.max_calls is not None and bucket.calls + count > quota.max_calls:
            return float("inf")
        if bucket.tokens >= count:
            return 0.0
        return round((count - bucket.tokens) / quota.rate, 6)

    def try_acquire(self, tool: str, *, count: int = 1) -> bool:
        """Take ``count`` calls from the bucket if they are available."""
        if count < 1:
            raise ValueError("count must be at least 1")
        quota = self._quota(tool)
        bucket = self._bucket(tool)
        if quota.max_calls is not None and bucket.calls + count > quota.max_calls:
            return False
        if bucket.tokens < count:
            return False
        bucket.tokens -= count
        bucket.calls += count
        return True

    def acquire(self, tool: str, *, count: int = 1) -> None:
        """Take ``count`` calls or raise :class:`QuotaExceeded`."""
        if self.try_acquire(tool, count=count):
            return
        wait = self.wait_time(tool, count=count)
        raise QuotaExceeded(tool, wait, permanent=wait == float("inf"))

    # -- housekeeping ----------------------------------------------------

    def calls(self, tool: str) -> int:
        """How many calls have been taken for this tool since the last reset."""
        bucket = self._buckets.get(tool)
        return bucket.calls if bucket else 0

    def reset(self, tool: str | None = None) -> None:
        """Refill one tool, or every tool, for a new run."""
        if tool is None:
            self._buckets.clear()
        else:
            self._buckets.pop(tool, None)

    def report(self) -> dict[str, dict[str, float | int]]:
        """A snapshot for a log line at the end of a run."""
        return {
            tool: {
                "calls": bucket.calls,
                "available": round(bucket.tokens, 6),
            }
            for tool, bucket in self._buckets.items()
        }
