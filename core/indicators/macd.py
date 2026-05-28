"""MACD(fast, slow, signal)."""
from __future__ import annotations

from collections.abc import Sequence

from .moving_average import ema


def macd(
    closes: Sequence[float],
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Return ``(macd_line, signal_line, histogram)``.

    각 리스트 길이는 입력과 동일. 워밍업 구간은 ``None``.
    ``signal_line``과 ``histogram``은 ``slow - 1 + signal - 1`` 인덱스부터 의미를 갖는다.
    """
    if not (0 < fast < slow):
        raise ValueError("0 < fast < slow required")
    if signal <= 0:
        raise ValueError("signal must be > 0")
    n = len(closes)
    none_n: list[float | None] = [None] * n
    if n < slow:
        return list(none_n), list(none_n), list(none_n)

    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line: list[float | None] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(ema_fast, ema_slow)
    ]

    # signal_line은 macd_line이 유효해진 지점부터 ``signal`` 개의 시드로 EMA 계산.
    start = slow - 1
    macd_tail = [v for v in macd_line[start:] if v is not None]
    sig_tail = ema(macd_tail, signal)

    signal_line: list[float | None] = [None] * n
    for offset, value in enumerate(sig_tail):
        idx = start + offset
        if idx < n:
            signal_line[idx] = value

    histogram: list[float | None] = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(macd_line, signal_line)
    ]
    return macd_line, signal_line, histogram
