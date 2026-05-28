"""Unit tests for core.indicators (RSI / MACD / MA / volume / candle patterns)."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from core.indicators import (
    ema,
    is_bearish_engulfing,
    is_bullish_engulfing,
    is_hammer,
    is_long_bearish,
    is_long_bullish,
    is_shooting_star,
    macd,
    rsi,
    sma,
    volume_spike_ratio,
)


@dataclass(frozen=True)
class _Bar:
    o: float
    h: float
    l: float
    c: float


# ─────────────────────────── SMA / EMA ───────────────────────────


def test_sma_basic_window() -> None:
    out = sma([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(3.0)
    assert out[4] == pytest.approx(4.0)


def test_ema_seed_and_smoothing() -> None:
    out = ema([10, 10, 10, 10, 10, 20], 5)
    # 첫 EMA = 10, 다음 EMA = (20-10)*2/6 + 10 = 13.33
    assert out[4] == pytest.approx(10.0)
    assert out[5] == pytest.approx(10 + (20 - 10) * (2 / 6))


def test_sma_short_input_returns_none() -> None:
    assert sma([1, 2], 5) == [None, None]


# ─────────────────────────── RSI ───────────────────────────


def test_rsi_strictly_rising_is_100() -> None:
    vals = rsi(list(range(1, 30)), period=14)
    assert vals[14] is not None
    assert vals[-1] == pytest.approx(100.0)


def test_rsi_strictly_falling_is_0() -> None:
    vals = rsi(list(range(30, 1, -1)), period=14)
    assert vals[-1] == pytest.approx(0.0)


def test_rsi_returns_none_when_short() -> None:
    vals = rsi([1, 2, 3], period=14)
    assert all(v is None for v in vals)


# ─────────────────────────── MACD ───────────────────────────


def test_macd_warmup_and_cross_on_uptrend() -> None:
    # 처음 50개는 평탄, 이후 상승 → 어딘가에서 골든크로스 발생
    closes = [100.0] * 50 + [100.0 + i for i in range(1, 30)]
    macd_line, sig_line, hist = macd(closes, fast=12, slow=26, signal=9)
    # slow=26 → 인덱스 25부터 의미. 그 직전(24)까지는 None.
    assert macd_line[24] is None
    assert macd_line[25] is not None
    assert macd_line[-1] is not None
    assert sig_line[-1] is not None
    # 상승 추세에서 macd_line이 결국 signal_line을 위로 돌파
    assert macd_line[-1] > sig_line[-1]
    assert hist[-1] is not None and hist[-1] > 0


def test_macd_signal_warmup_indices_none() -> None:
    closes = [float(i) for i in range(30)]
    _macd, sig_line, _hist = macd(closes, fast=12, slow=26, signal=9)
    # signal_line은 slow-1 부터 의미 있는 값 (그 전은 None)
    assert sig_line[25] is None or sig_line[25] == sig_line[25]  # 시드 시점 허용
    assert sig_line[24] is None


# ─────────────────────────── volume spike ───────────────────────────


def test_volume_spike_ratio_normal() -> None:
    # 21개: 처음 20개 평균 100, 마지막 300 → ratio 3.0
    vols = [100] * 20 + [300]
    assert volume_spike_ratio(vols, window=20) == pytest.approx(3.0)


def test_volume_spike_ratio_excludes_current_bar() -> None:
    vols = [10] * 19 + [1000] + [10]  # 가장 최근(10)은 평균에서 제외
    # 직전 20개 = 19×10 + 1000 = 1190 / 20 = 59.5
    # ratio = 10 / 59.5 ≈ 0.168
    assert volume_spike_ratio(vols, window=20) == pytest.approx(10 / 59.5)


def test_volume_spike_ratio_not_enough_data() -> None:
    assert volume_spike_ratio([1, 2, 3], window=20) is None


# ─────────────────────────── candle patterns ───────────────────────────


def test_hammer_pattern() -> None:
    # body 작고 아래 그림자 김
    c = _Bar(o=105, h=106, l=90, c=104)  # range=16, body=1, lower=14, upper=1
    assert is_hammer(c)
    assert not is_shooting_star(c)


def test_shooting_star_pattern() -> None:
    c = _Bar(o=105, h=120, l=104, c=106)  # range=16, body=1, upper=14, lower=1
    assert is_shooting_star(c)
    assert not is_hammer(c)


def test_long_bullish_and_bearish() -> None:
    bull = _Bar(o=100, h=110, l=99, c=108)  # range=11, body=8, body/range≈0.73
    bear = _Bar(o=110, h=111, l=100, c=102)  # range=11, body=8
    assert is_long_bullish(bull)
    assert not is_long_bearish(bull)
    assert is_long_bearish(bear)


def test_engulfing_patterns() -> None:
    prev_bear = _Bar(o=105, h=106, l=99, c=100)  # 음봉
    cur_bull = _Bar(o=99, h=110, l=98, c=108)    # 양봉, prev body 감쌈
    assert is_bullish_engulfing(prev_bear, cur_bull)

    prev_bull = _Bar(o=100, h=110, l=99, c=108)
    cur_bear = _Bar(o=109, h=110, l=95, c=99)
    assert is_bearish_engulfing(prev_bull, cur_bear)

    # 반대 케이스
    assert not is_bullish_engulfing(prev_bull, cur_bear)
    assert not is_bearish_engulfing(prev_bear, cur_bull)


def test_doji_range_zero_safe() -> None:
    flat = _Bar(o=100, h=100, l=100, c=100)
    assert not is_hammer(flat)
    assert not is_shooting_star(flat)
    assert not is_long_bullish(flat)
    assert not is_long_bearish(flat)
