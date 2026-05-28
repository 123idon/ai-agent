"""Unit tests for SignalAnalyzer (CLAUDE.md §5.2 진입 강도 판정)."""
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
    )
    base.update(overrides)
    return SignalParams(**base)


def _flat_bars(n: int, *, price: float = 100, vol: int = 100) -> list[Bar]:
    """평탄한 캔들 n개 (지표가 모두 중립값을 갖도록)."""
    return [Bar(o=price, h=price, l=price, c=price, v=vol) for _ in range(n)]


def _uptrend_bars(n: int, *, start: float = 100.0, step: float = 0.5, vol: int = 100) -> list[Bar]:
    out: list[Bar] = []
    for i in range(n):
        c = start + step * i
        out.append(Bar(o=c - step * 0.3, h=c + step * 0.5, l=c - step * 0.4, c=c, v=vol))
    return out


def _downtrend_bars(n: int, *, start: float = 200.0, step: float = 0.5, vol: int = 100) -> list[Bar]:
    out: list[Bar] = []
    for i in range(n):
        c = start - step * i
        out.append(Bar(o=c + step * 0.3, h=c + step * 0.4, l=c - step * 0.5, c=c, v=vol))
    return out


# ─────────────────────────── strong long entry ───────────────────────────


def test_long_entry_passes_volume_ma_candle() -> None:
    """상승추세 + 거래량 폭발(양봉) + 강세 장악형 → 거래량/MA/캔들 3개 지표가 통과한다.

    RSI/MACD는 직전 캔들과의 상승-전환·골든크로스 조건이 함께 잡혀야 하므로
    단순 합성 상승 추세에서는 누락될 수 있다. 본 테스트는 명시 가능한 3개 지표만
    검증하고, 결과 신호가 최소 CONDITIONAL 이상임을 확인한다.
    """
    analyzer = SignalAnalyzer(_params())

    bars = _uptrend_bars(80, start=100.0, step=0.5, vol=100)
    # 마지막 캔들 직전을 음봉으로 만들어 bullish_engulfing 패턴 생성
    bars[-2] = Bar(o=139.5, h=140.0, l=139.0, c=139.2, v=100)
    # 마지막은 거래량 4배 + 장대양봉 + 직전 음봉을 감쌈
    bars[-1] = Bar(o=139.0, h=141.5, l=138.9, c=141.4, v=400)

    decision = analyzer.evaluate("005930", bars, direction=Direction.LONG)
    by_name = {s.name: s for s in decision.scores}

    assert by_name["volume"].passed, by_name["volume"].detail
    assert by_name["ma"].passed, by_name["ma"].detail
    assert by_name["candle"].passed, by_name["candle"].detail
    assert decision.score_count >= 3
    assert decision.signal in (Signal.STRONG_ENTRY, Signal.CONDITIONAL_ENTRY)
    assert decision.entry_candle_low == 138
    assert decision.entry_candle_high == 141


# ─────────────────────────── no entry ───────────────────────────


def test_no_entry_on_flat_market() -> None:
    """평탄한 시장에서는 어떤 지표도 발동하지 않아야 한다."""
    analyzer = SignalAnalyzer(_params())
    decision = analyzer.evaluate(
        "005930", _flat_bars(80), direction=Direction.LONG,
    )
    assert decision.signal == Signal.NO_ENTRY
    assert decision.score_count == 0


def test_long_direction_rejects_downtrend() -> None:
    analyzer = SignalAnalyzer(_params())
    bars = _downtrend_bars(80, start=200.0, step=0.5)
    decision = analyzer.evaluate("005930", bars, direction=Direction.LONG)
    assert decision.signal == Signal.NO_ENTRY


# ─────────────────────────── short direction ───────────────────────────


def test_short_direction_on_downtrend() -> None:
    """하락추세는 SHORT 방향 평가에서 일부 지표가 통과해야 한다."""
    analyzer = SignalAnalyzer(_params())
    bars = _downtrend_bars(80, start=200.0, step=0.5)
    # 마지막 캔들 직전 양봉, 마지막은 거래량 폭발 + 장대음봉 + 감쌈
    bars[-2] = Bar(o=160.5, h=161.0, l=160.4, c=160.8, v=100)
    bars[-1] = Bar(o=161.0, h=161.1, l=158.5, c=158.6, v=400)

    decision = analyzer.evaluate("005930", bars, direction=Direction.SHORT)
    # SHORT 방향 평가에서 다수 지표 통과
    assert decision.score_count >= 3
    assert decision.signal in (Signal.STRONG_ENTRY, Signal.CONDITIONAL_ENTRY)


# ─────────────────────────── data shortage ───────────────────────────


def test_short_candle_history_gives_no_entry() -> None:
    analyzer = SignalAnalyzer(_params())
    decision = analyzer.evaluate("005930", _flat_bars(10), direction=Direction.LONG)
    # 60MA 필요 → 모든 지표 데이터 부족 처리
    assert decision.signal == Signal.NO_ENTRY
    assert all(not s.passed for s in decision.scores)


def test_empty_candles_raises() -> None:
    analyzer = SignalAnalyzer(_params())
    with pytest.raises(ValueError):
        analyzer.evaluate("005930", [], direction=Direction.LONG)


# ─────────────────────────── classification thresholds ───────────────────────────


def test_conditional_classification_at_threshold() -> None:
    """3개 충족 = CONDITIONAL, 2개 = NO_ENTRY, 4개 = STRONG. 분류기 단독 검증."""
    analyzer = SignalAnalyzer(_params())
    assert analyzer._classify(4) == Signal.STRONG_ENTRY
    assert analyzer._classify(3) == Signal.CONDITIONAL_ENTRY
    assert analyzer._classify(2) == Signal.NO_ENTRY
    assert analyzer._classify(0) == Signal.NO_ENTRY


# ─────────────────────────── reason text ───────────────────────────


def test_reason_text_lists_passed_indicators() -> None:
    analyzer = SignalAnalyzer(_params())
    bars = _uptrend_bars(80, start=100.0, step=0.5, vol=100)
    bars[-2] = Bar(o=139.5, h=140.0, l=139.0, c=139.2, v=100)
    bars[-1] = Bar(o=139.0, h=141.5, l=138.9, c=141.4, v=400)
    decision = analyzer.evaluate("005930", bars, direction=Direction.LONG)
    assert "LONG" in decision.reason_text
    assert decision.signal.value in decision.reason_text


# ─────────────────────────── config loader ───────────────────────────


def test_signal_params_loads_from_repo_yaml() -> None:
    params = SignalParams.from_file(
        Path(__file__).parents[2] / "config" / "strategy_params.yaml"
    )
    assert params.volume_surge_multiplier == 2.0
    assert params.rsi_period == 14
    assert params.macd_fast == 12 and params.macd_slow == 26 and params.macd_signal == 9
    assert params.ma_periods == (5, 20, 60)
    assert params.strong_min_indicators == 4
    assert params.conditional_min_indicators == 3
    assert "hammer" in params.candle_long
    assert "shooting_star" in params.candle_short
