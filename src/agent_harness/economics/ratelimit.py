"""Per-tenant token bucket, plus a circuit breaker for the upstream provider.

Two different jobs that people often conflate:

**Rate limiting protects you from your callers.** One tenant in a retry storm
must not consume the quota of every other tenant. The bucket is per tenant and
refills continuously, so a caller that behaves gets predictable service.

**The circuit breaker protects your caller from the upstream.** When the
provider is down, sending it more traffic makes recovery slower and makes your
own latency worse — every request pays the full timeout before failing. After
N consecutive failures the breaker opens and requests fail immediately for a
cooldown, then one probe decides whether to close it again.

Both are in-process. With several replicas the bucket becomes per replica,
which is usually fine (divide the limit by the replica count) until it is not
— at which point the same interface goes in front of Redis.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucket:
    """Continuous-refill bucket keyed by tenant."""

    def __init__(self, *, rate_per_minute: float, burst: int) -> None:
        self.rate_per_s = rate_per_minute / 60.0
        self.burst = float(burst)
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def allow(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds).

        rate_per_minute == 0 disables the limiter entirely, which is what the
        test suite and local development want.
        """
        if self.rate_per_s <= 0:
            return True, 0.0
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self.burst, updated_at=now)
                self._buckets[key] = bucket
            elapsed = now - bucket.updated_at
            bucket.tokens = min(self.burst, bucket.tokens + elapsed * self.rate_per_s)
            bucket.updated_at = now
            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True, 0.0
            deficit = cost - bucket.tokens
            return False, round(deficit / self.rate_per_s, 3)


class BreakerState(str, Enum):
    CLOSED = "closed"  # traffic flows
    OPEN = "open"  # traffic refused immediately
    HALF_OPEN = "half_open"  # one probe allowed


@dataclass(slots=True)
class CircuitBreaker:
    """Consecutive-failure breaker with a cooldown and a single probe.

    Only failures the provider is responsible for should be reported here —
    a guardrail block or a tool error says nothing about the model's health.
    """

    failure_threshold: int = 5
    cooldown_s: float = 30.0
    _state: BreakerState = field(default=BreakerState.CLOSED, init=False)
    _failures: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def state(self) -> BreakerState:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        if (
            self._state is BreakerState.OPEN
            and time.monotonic() - self._opened_at >= self.cooldown_s
        ):
            self._state = BreakerState.HALF_OPEN

    def allow(self) -> bool:
        with self._lock:
            self._maybe_half_open()
            return self._state is not BreakerState.OPEN

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = BreakerState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state is BreakerState.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = BreakerState.CLOSED
            self._opened_at = 0.0
