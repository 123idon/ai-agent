"""Unit tests for SignalAnalyzer — 일봉 게이트 + 5분봉 돌파 타점 (CLAUDE.md §5.2 개정)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from agents.analysis.signal.indicators import (
    Direction,
    Signal,
    SignalAnalyzer,
    SignalParams,
)


@dataclass(frozen=True)
class Bar:
    o: float
    h: float
    l: float
    c: float
    v: int


def _params(**overrides) -> SignalParams:
    base = dict(
        volume_surge_multiplier=2.0,
        rsi_period=14,
        rsi_oversold=30.0,
        rsi_overbought=70.0,
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
        ma_periods=(5, 20, 60),
        strong_min_indicators=4,
        conditional_min_indicators=3,
        candle_long=("hammer", "bullish_engulfing", "long_bullish"),
        candle_short=("shooting_star", "bearish_engulfing", "long_bearish"),
        rsi_entry_low=50.0,
        rsi_entry_high=65.0,
        breakout_lookback=5,
    )
    base.update(overrides)
    return SignalParams(**base)


def _up_min(
    n: int = 40, *, start: float = 10_000, step: float = 8, accel: float = 0.8, vol: int = 100,
) -> list[Bar]:
    """가속 상승 분봉 — 마지막은 거래량 4배 돌파 양봉(MACD 히스토그램 양 확대)."""
    bars: list[Bar] = []
    for i in range(n):
        c = round(start + step * i + accel * i * i)
        bars.append(Bar(o=c - 6, h=c + 4, l=c - 8, c=c, v=vol))
    last = bars[-1]
    bars[-1] = Bar(o=last.o, h=last.h, l=last.l, c=last.c, v=vol * 4)  # 거래량 4배
    return bars


def _flat(n: int = 40, *, price: float = 100, vol: int = 100) -> list[Bar]:
    return [Bar(o=price, h=price, l=price, c=price, v=vol) for _ in range(n)]


def _daily_pass(n: int = 25, *, base: float = 28_000, step: float = 20) -> list[Bar]:
    """일봉 게이트를 통과하는 일봉(완만 상승·양봉·마지막 거래량 급증)."""
    bars: list[Bar] = []
    for i in range(n):
        c = base + step * i
        bars.append(Bar(o=c - step * 0.5, h=c + step * 0.4, l=c - step * 0.6, c=c, v=100))
    last = bars[-1]
    bars[-1] = Bar(o=last.o, h=last.h, l=last.l, c=last.c, v=300)  # 전일 거래량 ↑
    return bars


# ─────────────────────────── 분봉 타점 ───────────────────────────


def test_minute_score_names_present() -> None:
    analyzer = SignalAnalyzer(_params())
    decision = analyzer.evaluate("005930", _up_min(), direction=Direction.LONG)
    names = {s.name for s in decision.scores}
    assert {"breakout", "volume", "rsi", "macd", "candle"} <= names


def test_breakout_and_volume_pass_on_uptrend() -> None:
    analyzer = SignalAnalyzer(_params())
    decision = analyzer.evaluate("005930", _up_min(), direction=Direction.LONG)
    by = {s.name: s for s in decision.scores}
    assert by["breakout"].passed          # 직전 고점 돌파
    assert by["volume"].passed            # 직전 캔들 대비 4배(≥200%)
    assert decision.score_count >= 3
    assert decision.signal in (Signal.STRONG_ENTRY, Signal.CONDITIONAL_ENTRY)


def test_strong_entry_when_rsi_band_widened() -> None:
    # RSI 상단을 100으로 넓히면 상승추세에서 4개 타점 전부 충족 → STRONG.
    analyzer = SignalAnalyzer(_params(rsi_entry_high=100.0))
    decision = analyzer.evaluate("005930", _up_min(), direction=Direction.LONG)
    assert decision.score_count == 4
    assert decision.signal == Signal.STRONG_ENTRY


def test_bearish_last_bar_blocks() -> None:
    analyzer = SignalAnalyzer(_params())
    bars = _up_min()
    last = bars[-1]
    bars[-1] = Bar(o=last.c, h=last.h, l=last.l - 50, c=last.c - 60, v=last.v)  # 음봉
    decision = analyzer.evaluate("005930", bars, direction=Direction.LONG)
    assert decision.signal == Signal.NO_ENTRY
    assert "음봉" in decision.reason_text


def test_flat_market_no_entry() -> None:
    analyzer = SignalAnalyzer(_params())
    decision = analyzer.evaluate("005930", _flat(), direction=Direction.LONG)
    assert decision.signal == Signal.NO_ENTRY


def test_short_history_no_entry() -> None:
    analyzer = SignalAnalyzer(_params())
    decision = analyzer.evaluate("005930", _flat(8), direction=Direction.LONG)
    assert decision.signal == Signal.NO_ENTRY


def test_empty_candles_raises() -> None:
    analyzer = SignalAnalyzer(_params())
    with pytest.raises(ValueError):
        analyzer.evaluate("005930", [], direction=Direction.LONG)


# ─────────────────────────── 일봉 게이트 ───────────────────────────


def test_eval_daily_gate_pass() -> None:
    # 과매수 차단을 끄면(상단 100) 완만 상승 일봉은 게이트 통과.
    analyzer = SignalAnalyzer(_params(daily_rsi_overbought=200.0))
    daily = _daily_pass()
    chk = analyzer._eval_daily(daily, price=10_000, direction=Direction.LONG)
    assert chk.sufficient
    assert chk.gate_passed
    assert not chk.blocked


def test_eval_daily_overbought_blocks() -> None:
    analyzer = SignalAnalyzer(_params(daily_rsi_overbought=70.0))
    daily = _daily_pass()                         # 가파른 상승 → 일봉 RSI 높음
    chk = analyzer._eval_daily(daily, price=10_000, direction=Direction.LONG)
    assert chk.blocked
    assert any("과매수" in r for r in chk.block_reasons)


def test_eval_daily_resistance_blocks() -> None:
    analyzer = SignalAnalyzer(_params(daily_rsi_overbought=200.0))
    daily = _daily_pass()
    res_high = max(b.h for b in daily[-20:])
    # 현재가가 저항 고점 -2% 이내 → 진입 금지.
    chk = analyzer._eval_daily(daily, price=res_high, direction=Direction.LONG)
    assert chk.blocked
    assert any("저항선" in r for r in chk.block_reasons)


def test_eval_daily_three_down_blocks() -> None:
    analyzer = SignalAnalyzer(_params(daily_rsi_overbought=200.0))
    daily = _daily_pass()
    # 마지막 3개를 음봉으로 교체 → 3일 연속 음봉 차단.
    for i in (-3, -2, -1):
        b = daily[i]
        daily[i] = Bar(o=b.c + 50, h=b.c + 60, l=b.c - 60, c=b.c, v=b.v)
    chk = analyzer._eval_daily(daily, price=10_000, direction=Direction.LONG)
    assert chk.blocked
    assert any("연속 음봉" in r for r in chk.block_reasons)


def test_eval_daily_insufficient_data() -> None:
    analyzer = SignalAnalyzer(_params())
    chk = analyzer._eval_daily(_daily_pass(10), price=10_000, direction=Direction.LONG)
    assert not chk.sufficient          # 20MA 등 산출 불가 → 미확인


# ─────────────────────────── 복합 통합 ───────────────────────────


def test_daily_gate_pass_sets_daily_strong() -> None:
    analyzer = SignalAnalyzer(_params(daily_rsi_overbought=200.0))
    decision = analyzer.evaluate(
        "005930", _up_min(), direction=Direction.LONG, daily_candles=_daily_pass(),
    )
    assert decision.daily_strong is True
    assert decision.signal in (Signal.STRONG_ENTRY, Signal.CONDITIONAL_ENTRY)


def test_daily_block_overrides_minute() -> None:
    # 분봉이 강해도 일봉 과매수면 진입 금지(NO_ENTRY).
    analyzer = SignalAnalyzer(_params(rsi_entry_high=100.0, daily_rsi_overbought=70.0))
    decision = analyzer.evaluate(
        "005930", _up_min(), direction=Direction.LONG, daily_candles=_daily_pass(),
    )
    assert decision.signal == Signal.NO_ENTRY
    assert "진입 안 함" in decision.reason_text


def test_no_daily_uses_minute_only() -> None:
    # 일봉 미제공 → 분봉만으로 판정, daily_strong=False(사이즈 보수화).
    analyzer = SignalAnalyzer(_params(rsi_entry_high=100.0))
    decision = analyzer.evaluate("005930", _up_min(), direction=Direction.LONG)
    assert decision.daily_strong is False
    assert decision.signal == Signal.STRONG_ENTRY


# ─────────────────────────── 분류 임계 ───────────────────────────


def test_classification_thresholds() -> None:
    analyzer = SignalAnalyzer(_params())
    assert analyzer._classify(4) == Signal.STRONG_ENTRY
    assert analyzer._classify(3) == Signal.CONDITIONAL_ENTRY
    assert analyzer._classify(2) == Signal.NO_ENTRY
    assert analyzer._classify(4, gated=True) == Signal.NO_ENTRY


# ─────────────────────────── 친화 사유 (요구 1) ───────────────────────────


def test_reason_text_is_friendly() -> None:
    analyzer = SignalAnalyzer(_params(rsi_entry_high=100.0))
    decision = analyzer.evaluate("005930", _up_min(), direction=Direction.LONG)
    assert "진입" in decision.reason_text
    # NO_ENTRY 사유는 "진입 안 함"으로 시작.
    flat = analyzer.evaluate("005930", _flat(), direction=Direction.LONG)
    assert "진입 안 함" in flat.reason_text


# ─────────────────────────── config loader ───────────────────────────


def test_signal_params_loads_from_repo_yaml() -> None:
    params = SignalParams.from_file(
        Path(__file__).parents[2] / "config" / "strategy_params.yaml"
    )
    assert params.rsi_period == 14
    assert params.macd_fast == 12 and params.macd_slow == 26 and params.macd_signal == 9
    assert params.ma_periods == (5, 20, 60)
    assert params.strong_min_indicators == 4
    assert params.conditional_min_indicators == 3
    # 아래 값들은 strategy_params.yaml(SSOT)의 funnel 완화 튜닝값을 추종한다(99c083e:
    # entry_zone 65→72, lookback 10→5, rsi_overbought 70→78, vol_mult 1.5→1.2,
    # resistance_pct 2%→0.5%; breakout.volume_mult 는 이후 회의 1adb141 로 5→4).
    # consult/회의로 이 키들을 다시 조정하면 이 단언도 함께 갱신해야 한다.
    assert params.rsi_entry_low == 50.0 and params.rsi_entry_high == 72.0
    assert params.breakout_lookback == 5
    assert params.breakout_volume_mult == 4.0
    assert params.daily_ma_period == 20
    assert params.daily_rsi_min == 50.0
    assert params.daily_rsi_overbought == 78.0
    assert params.daily_vol_mult == 1.2
    assert params.resistance_pct == 0.005
    assert params.consecutive_down == 3
