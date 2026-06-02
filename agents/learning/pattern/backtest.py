"""Backtest engine for paper-mode strategy validation (CLAUDE.md §11).

walk-forward 방식. 분봉 시퀀스에서 SignalAnalyzer로 진입 시그널을 평가하고,
설정된 익절/손절/트레일링 룰에 따라 청산을 시뮬레이션한다.
**타임스톱(시간 기반 매도)은 제거되었다** — 시간 경과만으로 청산하지 않는다.

가정/한계 (v1):
- 단일 종목, 단일 포지션 (보유 중에는 신규 진입 없음)
- 시장가 진입/청산 가정 (슬리피지/수수료 미반영)
- 분봉 종가 기준 의사결정
- 일일 손실 halt 없음 (§4.1)
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from math import sqrt
from statistics import mean, pstdev

from agents.analysis.signal.indicators import (
    Direction,
    Signal,
    SignalAnalyzer,
)
from core.indicators import CandleLike


class ExitReason(str, Enum):
    TP1 = "take_profit_1"
    TP2 = "take_profit_2"
    TRAILING = "trailing_stop"
    HARD_STOP = "hard_stop_loss"
    TECHNICAL = "technical_stop"
    END_OF_DATA = "end_of_data"


@dataclass(frozen=True)
class BacktestParams:
    initial_capital: float = 10_000_000.0
    position_size_pct: float = 0.30
    tp1_pct: float = 0.04                   # +4% 1차 익절
    tp1_close_ratio: float = 0.5
    tp2_pct: float = 0.07                   # +7% 2차 익절
    tp2_close_ratio: float = 0.3
    trailing_pct: float = 0.015             # 고점 -1.5% 트레일링
    hard_stop_pct: float = -0.03            # 하드 -3%
    use_technical_stop: bool = True         # 진입 캔들 저점 이탈 시 청산
    warmup_bars: int = 60


@dataclass(frozen=True)
class BacktestTrade:
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    qty: int
    pnl: float
    pnl_pct: float
    exit_reason: ExitReason
    signal: Signal


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    trades: list[BacktestTrade]
    initial_capital: float
    final_capital: float
    total_pnl: float
    total_pnl_pct: float
    win_count: int
    loss_count: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    max_drawdown_pct: float
    sharpe: float | None
    bar_count: int


@dataclass
class _OpenPosition:
    entry_idx: int
    entry_price: float
    qty_open: int
    qty_initial: int
    high_water: float
    signal: Signal
    entry_candle_low: float
    realized_pnl: float = 0.0


class BacktestEngine:
    """SignalAnalyzer 출력을 기반으로 walk-forward 시뮬레이션."""

    def __init__(
        self,
        analyzer: SignalAnalyzer,
        params: BacktestParams | None = None,
    ) -> None:
        self._analyzer = analyzer
        self._p = params or BacktestParams()

    def run(
        self,
        candles: Sequence[CandleLike],
        *,
        symbol: str = "TEST",
        direction: Direction = Direction.LONG,
    ) -> BacktestResult:
        if direction != Direction.LONG:
            raise NotImplementedError("backtest v1 supports LONG only")

        trades: list[BacktestTrade] = []
        capital = self._p.initial_capital
        position: _OpenPosition | None = None
        equity_curve: list[float] = [capital]
        peak_equity = capital

        warmup = max(self._p.warmup_bars, 1)
        for i in range(warmup, len(candles)):
            window = candles[: i + 1]
            cur = candles[i]

            if position is not None:
                position.high_water = max(position.high_water, float(cur.h))
                trade = self._maybe_exit(position, cur, i)
                if trade is not None:
                    capital += trade.pnl
                    trades.append(trade)
                    position = None
            else:
                decision = self._analyzer.evaluate(symbol, window, direction=direction)
                if decision.signal in (Signal.STRONG_ENTRY, Signal.CONDITIONAL_ENTRY):
                    entry_price = float(cur.c)
                    if entry_price > 0:
                        qty = int((capital * self._p.position_size_pct) // entry_price)
                        if qty > 0:
                            position = _OpenPosition(
                                entry_idx=i,
                                entry_price=entry_price,
                                qty_open=qty,
                                qty_initial=qty,
                                high_water=float(cur.h),
                                signal=decision.signal,
                                entry_candle_low=float(decision.entry_candle_low),
                            )

            mtm = capital + (
                position.realized_pnl
                + (float(cur.c) - position.entry_price) * position.qty_open
                if position is not None
                else 0.0
            )
            equity_curve.append(mtm)
            peak_equity = max(peak_equity, mtm)

        if position is not None:
            last_idx = len(candles) - 1
            last_price = float(candles[last_idx].c)
            trade = self._close_full(position, last_idx, last_price, ExitReason.END_OF_DATA)
            capital += trade.pnl
            trades.append(trade)
            equity_curve.append(capital)

        return self._summarize(symbol, trades, equity_curve, len(candles))

    def _maybe_exit(
        self, pos: _OpenPosition, cur: CandleLike, idx: int,
    ) -> BacktestTrade | None:
        price = float(cur.c)
        ret = (price - pos.entry_price) / pos.entry_price

        # 1) 하드 손절 (-3%)
        if ret <= self._p.hard_stop_pct:
            return self._close_full(pos, idx, price, ExitReason.HARD_STOP)

        # 2) 기술적 손절: 진입 캔들 저점 이탈
        if self._p.use_technical_stop and price < pos.entry_candle_low:
            return self._close_full(pos, idx, price, ExitReason.TECHNICAL)

        # 3) 트레일링 (1차 익절 도달 후에만 활성)
        had_tp1 = pos.qty_open < pos.qty_initial
        if had_tp1:
            drop_from_high = (pos.high_water - price) / pos.high_water
            if drop_from_high >= self._p.trailing_pct:
                return self._close_full(pos, idx, price, ExitReason.TRAILING)

        # 4) 익절 1차/2차 (단계적 부분 청산) — 단순화: 부분청산도 trade 1개로 합산
        if not had_tp1 and ret >= self._p.tp1_pct:
            return self._close_full(pos, idx, price, ExitReason.TP1)
        if had_tp1 and ret >= self._p.tp2_pct:
            return self._close_full(pos, idx, price, ExitReason.TP2)

        # (타임스톱 제거됨 — 시간 경과를 이유로 파는 청산은 존재하지 않는다.)
        return None

    def _close_full(
        self,
        pos: _OpenPosition,
        idx: int,
        price: float,
        reason: ExitReason,
    ) -> BacktestTrade:
        pnl = (price - pos.entry_price) * pos.qty_open + pos.realized_pnl
        pnl_pct = (price - pos.entry_price) / pos.entry_price if pos.entry_price else 0.0
        return BacktestTrade(
            entry_idx=pos.entry_idx,
            exit_idx=idx,
            entry_price=pos.entry_price,
            exit_price=price,
            qty=pos.qty_initial,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=reason,
            signal=pos.signal,
        )

    def _summarize(
        self,
        symbol: str,
        trades: list[BacktestTrade],
        equity_curve: list[float],
        bar_count: int,
    ) -> BacktestResult:
        total_pnl = sum(t.pnl for t in trades)
        final_capital = self._p.initial_capital + total_pnl
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]
        win_rate = (len(wins) / len(trades)) if trades else 0.0
        avg_win_pct = mean(t.pnl_pct for t in wins) if wins else 0.0
        avg_loss_pct = mean(t.pnl_pct for t in losses) if losses else 0.0

        # 최대 손실폭 (peak-to-trough %)
        peak = -float("inf")
        max_dd = 0.0
        for v in equity_curve:
            peak = max(peak, v)
            if peak > 0:
                dd = (peak - v) / peak
                max_dd = max(max_dd, dd)

        # Sharpe: 거래별 수익률의 평균/표준편차 × sqrt(거래 수)
        if len(trades) >= 2:
            rets = [t.pnl_pct for t in trades]
            sd = pstdev(rets)
            sharpe = (mean(rets) / sd * sqrt(len(rets))) if sd > 0 else None
        else:
            sharpe = None

        return BacktestResult(
            symbol=symbol,
            trades=trades,
            initial_capital=self._p.initial_capital,
            final_capital=final_capital,
            total_pnl=total_pnl,
            total_pnl_pct=(total_pnl / self._p.initial_capital) if self._p.initial_capital else 0.0,
            win_count=len(wins),
            loss_count=len(losses),
            win_rate=win_rate,
            avg_win_pct=avg_win_pct,
            avg_loss_pct=avg_loss_pct,
            max_drawdown_pct=max_dd,
            sharpe=sharpe,
            bar_count=bar_count,
        )
