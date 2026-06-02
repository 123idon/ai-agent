"""변동성 지표 — ATR (Average True Range).

CLAUDE.md §5.3의 ATR 기반 익절 목표가 동적 산출에 사용된다.
``score_volatility_atr``(screening)와 동일한 True Range 정의를 공유한다.
"""
from __future__ import annotations

from collections.abc import Sequence

from .candle import CandleLike


def true_ranges(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
) -> list[float]:
    """각 봉의 True Range = max(H-L, |H-prevC|, |L-prevC|). 길이 = len-1."""
    n = min(len(highs), len(lows), len(closes))
    trs: list[float] = []
    for i in range(1, n):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    return trs


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    period: int = 14,
) -> float | None:
    """단순이동평균 방식 ATR. 데이터가 부족하면 ``None``."""
    if period <= 0:
        return None
    if min(len(highs), len(lows), len(closes)) < period + 1:
        return None
    trs = true_ranges(highs, lows, closes)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def atr_pct(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    period: int = 14,
) -> float | None:
    """ATR / 마지막 종가. 종가가 0 이하이거나 데이터 부족 시 ``None``."""
    a = atr(highs, lows, closes, period=period)
    if a is None:
        return None
    last = closes[-1]
    if last <= 0:
        return None
    return a / last


def atr_pct_from_candles(
    candles: Sequence[CandleLike], *, period: int = 14,
) -> float | None:
    """캔들 시퀀스(o/h/l/c)에서 ATR% 산출."""
    if len(candles) < period + 1:
        return None
    highs = [float(c.h) for c in candles]   # type: ignore[attr-defined]
    lows = [float(c.l) for c in candles]    # type: ignore[attr-defined]
    closes = [float(c.c) for c in candles]
    return atr_pct(highs, lows, closes, period=period)
