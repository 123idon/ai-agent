"""Wilder-smoothed RSI."""
from __future__ import annotations

from collections.abc import Sequence


def rsi(closes: Sequence[float], period: int = 14) -> list[float | None]:
    """Wilder smoothing 방식 RSI.

    반환 길이는 입력 길이와 같으며, 워밍업 구간(앞 ``period`` 개)은 ``None``.
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(closes)
    result: list[float | None] = [None] * n
    if n < period + 1:
        return result

    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains[i] = diff
        elif diff < 0:
            losses[i] = -diff

    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period
    result[period] = _to_rsi(avg_gain, avg_loss)

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result[i] = _to_rsi(avg_gain, avg_loss)

    return result


def _to_rsi(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)
