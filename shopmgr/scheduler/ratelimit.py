"""令牌桶限速：单账号 QPS ≤ 0.3（任务书 4.3），允许少量突发以保证页面交互不卡顿。"""
from __future__ import annotations

import asyncio
import time
from typing import Callable


class TokenBucket:
    def __init__(self, rate: float, capacity: int, clock: Callable[[], float] = time.monotonic):
        self.rate = rate
        self.capacity = capacity
        self._clock = clock
        self._tokens = float(capacity)
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def reserve(self) -> float:
        """预占一个令牌，返回需要等待的秒数（0 表示立即可用）。"""
        self._refill()
        self._tokens -= 1
        if self._tokens >= 0:
            return 0.0
        return -self._tokens / self.rate

    async def acquire(self) -> float:
        async with self._lock:
            wait = self.reserve()
            if wait > 0:
                await asyncio.sleep(wait)
            return wait
