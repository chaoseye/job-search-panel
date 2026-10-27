from __future__ import annotations

import pytest

from app.hh.ratelimit import TokenBucket
from tests.conftest import FakeClock, FakeSleep


async def test_burst_then_wait() -> None:
    clock = FakeClock()
    sleep = FakeSleep()
    bucket = TokenBucket(rate=2.0, burst=2, clock=clock, sleep=sleep)
    await bucket.acquire()
    await bucket.acquire()
    assert sleep.calls == []
    await bucket.acquire()  # bucket empty -> wait for one token at 2 rps = 0.5 s
    assert sleep.calls == [pytest.approx(0.5)]
    assert bucket.waits == 1


async def test_refill_over_time() -> None:
    clock = FakeClock()
    sleep = FakeSleep()
    bucket = TokenBucket(rate=1.0, burst=1, clock=clock, sleep=sleep)
    await bucket.acquire()
    clock.advance(1.0)
    await bucket.acquire()
    assert sleep.calls == []


def test_rejects_non_positive_rate() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate=0)
