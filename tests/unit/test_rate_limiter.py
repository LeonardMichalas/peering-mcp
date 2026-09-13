"""The limiter is the promise we make to PeeringDB. These tests are that promise.

A fake clock is used throughout. Asserting the real behaviour of twenty callers
at one per second would otherwise take twenty seconds of wall time, which means
nobody would run it.
"""

from __future__ import annotations

import asyncio
from itertools import pairwise

import pytest

from peering_mcp.clients.rate_limit import RateLimiter


class FakeClock:
    """A monotonic clock that only moves when someone sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        assert seconds >= 0, "the limiter must never sleep a negative duration"
        self.now += seconds
        # Yield so other tasks can run, as a real sleep would.
        await asyncio.sleep(0)


def make_limiter(rate: float = 1.0) -> tuple[RateLimiter, FakeClock]:
    clock = FakeClock()
    return RateLimiter(rate, clock=clock.time, sleep=clock.sleep), clock


async def test_first_acquisition_does_not_wait() -> None:
    limiter, clock = make_limiter()
    start = clock.now

    await limiter.acquire()

    assert clock.now == start, "nothing should be throttled before the first request"


async def test_second_acquisition_waits_one_interval() -> None:
    limiter, clock = make_limiter()

    await limiter.acquire()
    await limiter.acquire()

    assert clock.now == pytest.approx(1001.0)


async def test_twenty_concurrent_callers_never_exceed_one_per_second() -> None:
    """The acceptance criterion for T3, asserted directly."""
    limiter, clock = make_limiter()
    acquired_at: list[float] = []

    async def caller() -> None:
        await limiter.acquire()
        acquired_at.append(clock.now)

    await asyncio.gather(*(caller() for _ in range(20)))

    assert len(acquired_at) == 20
    gaps = [b - a for a, b in pairwise(acquired_at)]
    assert all(gap >= limiter.interval - 1e-9 for gap in gaps), (
        f"the limiter released two callers less than {limiter.interval}s apart: {gaps}"
    )
    # Nineteen gaps of one second after a free first acquisition.
    assert acquired_at[-1] - acquired_at[0] == pytest.approx(19.0)


async def test_callers_are_released_in_arrival_order() -> None:
    limiter, _ = make_limiter()
    order: list[int] = []

    async def caller(index: int) -> None:
        await limiter.acquire()
        order.append(index)

    await asyncio.gather(*(caller(i) for i in range(10)))

    assert order == list(range(10)), "the limiter should be fair, not a lottery"


async def test_idle_time_is_not_banked() -> None:
    """Waiting a long time earns one request, not a burst of them."""
    limiter, clock = make_limiter()
    await limiter.acquire()

    clock.now += 60.0
    await limiter.acquire()
    first_after_idle = clock.now
    await limiter.acquire()

    assert clock.now - first_after_idle == pytest.approx(1.0)


async def test_rate_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(0)
