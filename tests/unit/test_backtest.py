"""Tests for backtest engine."""
from __future__ import annotations

from dataclasses import dataclass

from agents.analysis.signal.indicators import (
    Direction,
    Signal,
    SignalAnalyzer,
    SignalParams,
)
from agents.learning.pattern.backtest import (
    BacktestEngine,
    BacktestParams,
    ExitReason,
)


@dataclass(frozen=True)
class Bar:
    o: float
    h: float
    l: float
    c: float
    v: int


def _params() -> SignalParams:
    return SignalParams(
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


def _flat_bars(n: int) -> list[Bar]:
    return [Bar(o=100, h=100, l=100, c=100, v=100) for _ in range(n)]


def _bull_then_pullback() -> list[Bar]:
    """워밍업 후 신호 발생 + 이후 가격 +4% 상승 + 후속 하락으로 트레일링/손절 발동."""
    bars: list[Bar] = []
    # 80개 상승 추세 (정수 가격)
    for i in range(78):
        c = 10_000 + 50 * i
        bars.append(Bar(o=c - 30, h=c + 50, l=c - 40, c=c, v=100))
    # 음봉
    bars.append(Bar(o=13_950, h=14_000, l=13_900, c=13_920, v=100))
    # bullish engulfing + 거래량 spike (마지막 신호 발생)
    bars.append(Bar(o=13_900, h=14_150, l=13_890, c=14_140, v=400))
    # 진입 후 +5% 상승 (14_140 → 14_850 정도)
    target_high = int(14_140 * 1.06)
    for i in range(5):
        c = 14_140 + (target_high - 14_140) * (i + 1) // 5
        bars.append(Bar(o=c - 30, h=c + 50, l=c - 30, c=c, v=200))
    # 이후 큰 하락
    for i in range(5):
        c = target_high - (target_high - 13_500) * (i + 1) // 5
        bars.append(Bar(o=c + 30, h=c + 50, l=c - 30, c=c, v=200))
    return bars


def test_backtest_no_trade_on_flat() -> None:
    analyzer = SignalAnalyzer(_params())
    engine = BacktestEngine(analyzer, BacktestParams(warmup_bars=60))
    result = engine.run(_flat_bars(120))
    assert result.trades == []
    assert result.total_pnl == 0
    assert result.final_capital == result.initial_capital


def test_backtest_records_trade_on_signal() -> None:
    analyzer = SignalAnalyzer(_params())
    engine = BacktestEngine(
        analyzer,
        BacktestParams(warmup_bars=60, time_stop_bars=3, tp1_pct=0.04),
    )
    result = engine.run(_bull_then_pullback())
    assert len(result.trades) >= 1
    assert result.bar_count == len(_bull_then_pullback())


def test_backtest_summary_fields_present() -> None:
    analyzer = SignalAnalyzer(_params())
    engine = BacktestEngine(analyzer, BacktestParams(warmup_bars=60))
    result = engine.run(_bull_then_pullback())
    # 합성 결과를 정확히 단언하기 어려우니 구조와 일관성만 검증.
    assert result.win_count + result.loss_count <= len(result.trades)
    assert 0.0 <= result.win_rate <= 1.0
    assert result.max_drawdown_pct >= 0.0
    assert result.final_capital == result.initial_capital + result.total_pnl


def test_backtest_supports_only_long_in_v1() -> None:
    analyzer = SignalAnalyzer(_params())
    engine = BacktestEngine(analyzer)
    try:
        engine.run(_flat_bars(80), direction=Direction.SHORT)
    except NotImplementedError:
        return
    raise AssertionError("should raise NotImplementedError for SHORT")


def test_pattern_agent_backtest_proxy() -> None:
    from pathlib import Path
    from agents.learning.pattern.main import PatternAnalysisAgent
    from core.kis_client import Mode
    from core.messaging import Bus

    analyzer = SignalAnalyzer(_params())
    agent = PatternAnalysisAgent(Mode.PAPER, Bus(), Path("/tmp"), analyzer=analyzer)
    result = agent.backtest(_flat_bars(80))
    assert result.trades == []
