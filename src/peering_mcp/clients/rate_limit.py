"""A process-wide async rate limiter.

PeeringDB allows one request per second. The limiter lives in the client layer
rather than in the tools, so that a tool added later cannot forget to use it.

The clock and the sleep function are injectable. That is not indirection for
its own sake: it is the only way to assert the limiter's behaviour over twenty
callers without the test taking twenty seconds.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class RateLimiter:
    """Serialises callers so that acquisitions are at least `interval` apart.

    Fair by construction: callers are released in arrival order, because each
    waits on the same lock.
    """

    def __init__(
        self,
        requests_per_second: float,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if requests_per_second <= 0:
            msg = "requests_per_second must be positive"
            raise ValueError(msg)
        self._interval = 1.0 / requests_per_second
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._lock = asyncio.Lock()
        self._next_allowed: float | None = None

    @property
    def interval(self) -> float:
        """Minimum seconds between two acquisitions."""
        return self._interval

    async def acquire(self) -> None:
        """Block until the caller is allowed to make a request."""
        async with self._lock:
            now = self._clock()
            if self._next_allowed is not None and now < self._next_allowed:
                await self._sleep(self._next_allowed - now)
                now = self._clock()
            self._next_allowed = now + self._interval
