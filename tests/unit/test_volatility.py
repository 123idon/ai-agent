"""Unit tests for ATR volatility indicators (CLAUDE.md §5.3)."""
from __future__ import annotations

from dataclasses import dataclass

from core.indicators import atr, atr_pct, atr_pct_from_candles, true_ranges


@dataclass
class C:
    o: float
    h: float
    l: float
    c: float
    v: int = 0


def test_true_ranges_basic() -> None:
    highs = [10, 12, 11]
    lows = [9, 10, 9]
    closes = [9.5, 11, 10]
    # i=1: max(12-10, |12-9.5|, |10-9.5|)=2.5 ; i=2: max(11-9, |11-11|, |9-11|)=2
    assert true_ranges(highs, lows, closes) == [2.5, 2.0]


def test_atr_simple_average() -> None:
    highs = [10, 12, 11, 13]
    lows = [9, 10, 9, 11]
    closes = [9.5, 11, 10, 12]
    # TRs: i1=2.5, i2=2.0, i3=max(13-11,|13-10|,|11-10|)=3 → period2 avg of last2 = 2.5
    assert atr(highs, lows, closes, period=2) == 2.5


def test_atr_insufficient_data() -> None:
    assert atr([1, 2], [1, 1], [1, 2], period=14) is None


def test_atr_pct_ratio() -> None:
    highs = [100, 104, 102, 106]
    lows = [98, 100, 99, 101]
    closes = [99, 103, 100, 105]
    a = atr(highs, lows, closes, period=2)
    assert a is not None
    assert abs(atr_pct(highs, lows, closes, period=2) - a / 105) < 1e-12


def test_atr_pct_from_candles() -> None:
    candles = [C(10, 11, 9, 10), C(10, 12, 10, 11), C(11, 13, 11, 12)]
    val = atr_pct_from_candles(candles, period=2)
    assert val is not None and val > 0


def test_atr_pct_zero_close_guard() -> None:
    assert atr_pct([10, 11, 12], [9, 10, 11], [9, 10, 0], period=2) is None
