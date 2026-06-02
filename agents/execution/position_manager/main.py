"""실시간 포지션 매니저 (CLAUDE.md §2.5, §5.3~5.5, §5.7).

보유 중 단일 포지션을 주기적으로 점검하여 익절(3단)·기술적/하드 손절·
트레일링·EOD 강제 청산을 수행한다. **타임스톱(시간 기반 매도)은 제거되었다** —
시간 경과를 이유로 파는 일은 없다. 진입(매수)은 본 에이전트가 다루지
않으며, ``order.event``(매수 체결)를 구독해 포지션을 등록만 한다.

보호 청산은 §5.4 "무조건 청산" 원칙에 따라 **리스크 게이트를 우회**해 시장가로
즉시 송신한다(진입 게이트 HL-01/03/04/02는 청산과 무관). 손절류는 HL-02 연속
손절 카운터에 산입, 익절류는 카운터 리셋, EOD는 미산입(§5.5).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from agents.analysis.signal.indicators import KST, Direction, Signal, SignalAnalyzer
from agents.analysis.signal.main import EntrySignal
from agents.execution.order.main import OrderAgent, OrderEvent
from agents.execution.position_manager.exit_rules import (
    CounterEffect,
    ExitAction,
    ExitParams,
    LivePositionState,
    evaluate_exit,
    friendly_exit_reason,
    select_tp_targets,
)
from agents.risk.risk_manager.hard_limits import StopLossTracker
from agents.risk.risk_manager.main import ApprovedOrder
from core.indicators import macd as macd_calc, sma, volume_spike_ratio
from core.kis_client import (
    KisBusinessError,
    KisClient,
    KisTransportError,
    OrderType,
    Position,
    Side,
)
from core.messaging import Bus

log = logging.getLogger(__name__)

TOPIC_EXIT = "signal.exit"


@dataclass(frozen=True)
class ExitEvent:
    """청산 실행 결과 (학습부 journal + CEO 보고 대상)."""

    symbol: str
    kind: str
    ratio: float
    qty: int
    price: int
    reason: str
    pnl_pct: float
    counter: str
    use_credit: bool
    timestamp: datetime


class PositionManagerAgent:
    def __init__(
        self,
        kis: KisClient,
        bus: Bus,
        order_agent: OrderAgent,
        analyzer: SignalAnalyzer,
        params: ExitParams,
        stoploss_tracker: StopLossTracker,
        *,
        chart_tf: str = "1",
        poll_seconds: int = 20,
        clock: Callable[[], datetime] = lambda: datetime.now(KST),
    ) -> None:
        self._kis = kis
        self._bus = bus
        self._order = order_agent
        self._analyzer = analyzer
        self._p = params
        self._tracker = stoploss_tracker
        self._chart_tf = chart_tf
        self._poll_seconds = poll_seconds
        self._clock = clock

        # 종목코드 → 보유 포지션 상태 (동시 최대 3종목, HL-01 / 요구 2).
        self._states: dict[str, LivePositionState] = {}
        self._entry_signals: dict[str, EntrySignal] = {}

        self._bus.subscribe("order.event", self._on_order_event)

    # ─────────────────────────── 상태 ───────────────────────────

    def is_flat(self) -> bool:
        """보유 포지션이 하나도 없으면 True."""
        return not self._states

    def held_count(self) -> int:
        """현재 보유 종목 수 (진입 게이트 HL-01: < 3일 때만 신규)."""
        return len(self._states)

    def held_codes(self) -> set[str]:
        return set(self._states)

    def reset(self) -> None:
        """내부 포지션 상태 전체 초기화 (백테스트 리셋/새 런 시작 시)."""
        self._states.clear()
        self._entry_signals.clear()

    async def _on_order_event(self, event: OrderEvent) -> None:
        """매수 체결 시 포지션 등록. 매도(본 에이전트의 청산)는 monitor가 잔고로 반영."""
        if event.side != Side.BUY:
            return
        sig = event.approved.entry_signal
        existing = self._states.get(event.symbol)
        if existing is not None:
            # 동일 종목 추가 매수 — 수량만 합산(원 진입 근거/목표가 유지).
            existing.qty_open += event.qty
            existing.qty_initial += event.qty
            return
        self._entry_signals[event.symbol] = sig
        atr_pct = getattr(sig, "atr_pct", None)
        tp1_target, tp2_target = select_tp_targets(atr_pct, self._p)
        self._states[event.symbol] = LivePositionState(
            symbol=event.symbol,
            entry_price=float(event.price),
            entry_candle_low=float(sig.entry_candle_low),
            qty_initial=event.qty,
            qty_open=event.qty,
            high_water=float(event.price),
            entry_time=event.timestamp,
            use_credit=event.use_credit,
            tp1_target=tp1_target,
            tp2_target=tp2_target,
        )
        log.info(
            "POSITION_OPEN %s qty=%d entry=%d low=%d atr=%s TP1=%.1f%% TP2=%.1f%% (보유 %d종목)",
            event.symbol, event.qty, event.price, sig.entry_candle_low,
            f"{atr_pct:.2%}" if atr_pct is not None else "n/a",
            tp1_target * 100, tp2_target * 100, len(self._states),
        )

    # ─────────────────────────── 모니터 ───────────────────────────

    async def monitor_once(self) -> ExitEvent | None:
        """보유 전 종목을 점검해 청산을 수행. 마지막 청산 이벤트를 반환(없으면 None)."""
        try:
            balance = await self._kis.get_balance()
        except (KisBusinessError, KisTransportError) as e:
            log.warning("position monitor: balance fetch failed: %s", e)
            return None

        held = {p.code: p for p in balance.positions if p.qty > 0}

        # 1) 잔고에서 사라진 종목(전량 청산 완료)은 내부 상태에서 제거
        for code in list(self._states):
            if code not in held:
                log.info("POSITION_CLOSED %s (잔고에서 사라짐)", code)
                self._clear(code)

        # 2) 등록되지 않은 보유 포지션 입양(세션 재시작/외부 매수 대비)
        for code, p in held.items():
            if code not in self._states:
                self._adopt(p)

        # 3) 각 보유 종목 평가 — 청산 발생 시 마지막 이벤트를 반환
        last_ev: ExitEvent | None = None
        for code, p in list(held.items()):
            pos = self._states.get(code)
            if pos is None:
                continue
            ev = await self._evaluate_one(pos, p)
            if ev is not None:
                last_ev = ev
        return last_ev

    async def _evaluate_one(
        self, pos: LivePositionState, p: Position,
    ) -> ExitEvent | None:
        # 잔고 권위 보정 (수량/신용여부)
        pos.qty_open = p.qty
        pos.use_credit = pos.use_credit or bool(p.loanDt)

        # 1분봉으로 현재가·고점·신호붕괴 산출
        try:
            chart = await self._kis.get_chart(pos.symbol, tf=self._chart_tf)
        except (KisBusinessError, KisTransportError) as e:
            log.warning("position monitor: chart fetch failed %s: %s", pos.symbol, e)
            return None
        candles = chart.candles
        if not candles:
            return None
        last = candles[-1]
        price = float(last.c)
        pos.high_water = max(pos.high_water, float(last.h), price)

        now = self._clock()
        # 신호붕괴 청산은 제거됨(§5.2) — 손절은 기술적/하드, 익절은 ATR/트레일링만.
        # EOD 강제청산(15:20)은 evaluate_exit가 어떤 조건에서도 예외 없이 적용(§5.7).
        action = evaluate_exit(
            pos, price=price, now=now, breakdown_count=0, params=self._p,
        )
        if not action.is_exit:
            return None
        return await self._do_exit(pos, action, price, now)

    async def _do_exit(
        self, pos: LivePositionState, action: ExitAction, price: float, now: datetime,
    ) -> ExitEvent | None:
        sell_qty = (
            pos.qty_open if action.ratio >= 1.0
            else max(1, int(pos.qty_open * action.ratio))
        )
        sell_qty = min(sell_qty, pos.qty_open)
        if sell_qty <= 0:
            return None

        # 손익률 사전 산출 → 쉬운 한국어 사유로 변환(요구 1).
        pnl_pct = (price - pos.entry_price) / pos.entry_price if pos.entry_price else 0.0
        friendly = friendly_exit_reason(
            action.kind, pnl_pct, ratio=action.ratio,
        )

        order = ApprovedOrder(
            symbol=pos.symbol,
            side=Side.SELL,
            code=pos.symbol,
            qty=sell_qty,
            price=0,                       # 시장가 (보호 청산)
            order_type=OrderType.MARKET,
            use_credit=pos.use_credit,
            is_new_entry=False,            # 청산 — 진입 게이트 면제
            entry_signal=self._exit_trace_signal(pos, now, friendly),
            timestamp=now,
            reason=friendly,
        )
        result = await self._order.execute(order)
        if not isinstance(result, OrderEvent):
            log.error("EXIT order failed %s: %s", pos.symbol, action.kind.value)
            return None  # 다음 폴링에서 재시도

        # 카운터 갱신 (HL-02 연동)
        if action.counter == CounterEffect.STOPLOSS:
            self._tracker.record_stoploss(now)
        elif action.counter == CounterEffect.TAKEPROFIT:
            self._tracker.record_take_profit()

        # 상태 갱신
        if action.ratio >= 1.0:
            self._clear(pos.symbol)
        else:
            pos.qty_open = max(0, pos.qty_open - sell_qty)
            if action.kind.value == "take_profit_1":
                pos.tp1_done = True
            elif action.kind.value == "take_profit_2":
                pos.tp2_done = True

        ev = ExitEvent(
            symbol=pos.symbol,
            kind=action.kind.value,
            ratio=action.ratio,
            qty=sell_qty,
            price=int(price),
            reason=friendly,               # 쉬운 한국어 사유(요구 1)
            pnl_pct=pnl_pct,
            counter=action.counter.value,
            use_credit=pos.use_credit,
            timestamp=now,
        )
        log.info(
            "EXIT %s %s qty=%d price=%d pnl=%+.2f%% counter=%s | %s",
            pos.symbol, action.kind.value, sell_qty, int(price),
            pnl_pct * 100, action.counter.value, friendly,
        )
        await self._bus.publish(TOPIC_EXIT, ev)
        return ev

    # ─────────────────────────── 헬퍼 ───────────────────────────

    def _clear(self, symbol: str) -> None:
        self._states.pop(symbol, None)
        self._entry_signals.pop(symbol, None)

    def _adopt(self, p: Position) -> None:
        """등록 안 된 보유 포지션을 보수적으로 입양.

        entry_candle_low를 모르므로 기술적 손절은 비활성(0)하고 하드 손절·EOD만
        적용한다. 잔고 이월(요구 3)로 다음날 이어받은 종목도 이 경로로 익일
        시초가 기준(entry_time=now)으로 재평가된다.
        """
        now = self._clock()
        entry = float(p.avgPrice) if p.avgPrice else float(p.currentPrice)
        self._states[p.code] = LivePositionState(
            symbol=p.code,
            entry_price=entry,
            entry_candle_low=0.0,          # 기술적 손절 비활성 (저점 미상)
            qty_initial=p.qty,
            qty_open=p.qty,
            high_water=float(p.currentPrice or entry),
            entry_time=now,
            use_credit=bool(p.loanDt),
        )
        self._entry_signals.pop(p.code, None)
        log.warning(
            "POSITION_ADOPT %s qty=%d avg=%d (기술적 손절 비활성)",
            p.code, p.qty, p.avgPrice,
        )

    def _exit_trace_signal(
        self, pos: LivePositionState, now: datetime, reason: str,
    ) -> EntrySignal:
        sig = self._entry_signals.get(pos.symbol)
        if sig is not None:
            return sig
        # 입양 포지션 등 원 신호가 없는 경우 trace용 최소 신호 합성
        return EntrySignal(
            symbol=pos.symbol,
            direction=Direction.LONG,
            signal=Signal.NO_ENTRY,
            score_count=0,
            entry_price=int(pos.entry_price),
            entry_candle_low=int(pos.entry_candle_low),
            entry_candle_high=int(pos.entry_price),
            use_credit_hint=pos.use_credit,
            timestamp=now,
            reason=f"adopted-position exit ({reason})",
        )

    def _breakdown_count(self, candles, price: float) -> tuple[int, list[str]]:
        """진입 근거 붕괴 신호 카운트 (§5.4): 거래량 소멸/MACD 데드/5MA 이탈."""
        closes = [float(c.c) for c in candles]
        volumes = [int(c.v) for c in candles]
        params = self._analyzer.params
        count = 0
        notes: list[str] = []

        short_p = params.ma_periods[0]
        if len(closes) >= short_p:
            ma_short = sma(closes, short_p)[-1]
            if ma_short is not None and price < ma_short:
                count += 1
                notes.append(f"{short_p}MA이탈")

        warmup = params.macd_slow + params.macd_signal
        if len(closes) >= warmup:
            macd_line, sig_line, _ = macd_calc(
                closes,
                fast=params.macd_fast,
                slow=params.macd_slow,
                signal=params.macd_signal,
            )
            if (
                macd_line[-1] is not None
                and sig_line[-1] is not None
                and macd_line[-1] < sig_line[-1]
            ):
                count += 1
                notes.append("MACD데드")

        if len(volumes) >= 21:
            ratio = volume_spike_ratio(volumes, window=20)
            if ratio is not None and ratio < 1.0:
                count += 1
                notes.append("거래량소멸")

        return count, notes

    # ─────────────────────────── 루프 ───────────────────────────

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.monitor_once()
            except Exception:
                log.exception("position monitor failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_seconds)
            except asyncio.TimeoutError:
                continue
