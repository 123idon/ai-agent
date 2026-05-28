"""Simple and exponential moving averages.

반환 길이는 항상 입력 길이와 같다. 워밍업 구간은 ``None`` 으로 채워서
인덱스를 캔들 시퀀스와 동기화한다.
"""
from __future__ import annotations

from collections.abc import Sequence


def sma(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(values)
    result: list[float | None] = [None] * n
    if n < period:
        return result
    running = sum(values[:period])
    result[period - 1] = running / period
    for i in range(period, n):
        running += values[i] - values[i - period]
        result[i] = running / period
    return result


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """첫 ``period`` 개의 단순 평균을 시드로 사용한 EMA."""
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(values)
    result: list[float | None] = [None] * n
    if n < period:
        return result
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    k = 2.0 / (period + 1)
    prev = seed
    for i in range(period, n):
        cur = (values[i] - prev) * k + prev
        result[i] = cur
        prev = cur
    return result
