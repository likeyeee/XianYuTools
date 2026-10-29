"""指数退避：基数 1.5s，上限 60s（任务书 4.3），附加 0–20% 抖动避免同时重试。"""
from __future__ import annotations

import random


def backoff_delay(attempt: int, base: float = 1.5, cap: float = 60.0, jitter: float = 0.2,
                  rnd: random.Random | None = None) -> float:
    """attempt 从 1 开始：1.5, 3, 6, 12 ... 封顶 cap。"""
    raw = min(cap, base * (2 ** max(0, attempt - 1)))
    r = (rnd or random).random()
    return min(cap, raw * (1 + jitter * r))
