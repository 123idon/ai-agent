"""Signal analysis agent (CLAUDE.md §2.3).

분봉 캔들을 받아 5지표를 평가하고, ``Signal.NO_ENTRY``가 아니면
``signal.entry`` 토픽으로 ``EntrySignal``을 발행한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from core.kis_client import KisClient
from core.messaging import Bus

from .indicators import Direction, Signal, SignalAnalyzer

log = logging.getLogger(__name__)

TOPIC_ENTRY = "signal.entry"


@dataclass(frozen=True)
class EntrySignal:
    """분석부 → 리스크부 페이로드."""

    symbol: str
    direction: Direction
    signal: Signal
    score_count: int
    entry_price: int            # 분석부 권장 진입가 (지정가)
    entry_candle_low: int       # §5.4 기술적 손절 기준
    entry_candle_high: int
    use_credit_hint: bool       # 분석부 권고 (실제 결정은 리스크부)
    timestamp: datetime
    reason: str


class SignalAgent:
    def __init__(
        self,
        kis: KisClient,
        analyzer: SignalAnalyzer,
        bus: Bus,
        *,
        chart_tf: str = "1",
        use_credit_default: bool = False,
    ) -> None:
        self._kis = kis
        self._analyzer = analyzer
        self._bus = bus
        self._chart_tf = chart_tf
        self._use_credit_default = use_credit_default

    async def analyze_symbol(
        self,
        code: str,
        *,
        direction: Direction = Direction.LONG,
    ) -> EntrySignal | None:
        chart = await self._kis.get_chart(code, tf=self._chart_tf)
        if not chart.candles:
            log.info("no candles for %s", code)
            return None

        decision = self._analyzer.evaluate(code, chart.candles, direction=direction)
        if decision.signal == Signal.NO_ENTRY:
            log.info("NO_ENTRY %s: %s", code, decision.reason_text)
            return None

        last = chart.candles[-1]
        signal = EntrySignal(
            symbol=code,
            direction=decision.direction,
            signal=decision.signal,
            score_count=decision.score_count,
            entry_price=int(last.c),
            entry_candle_low=decision.entry_candle_low,
            entry_candle_high=decision.entry_candle_high,
            use_credit_hint=self._use_credit_default,
            timestamp=decision.timestamp,
            reason=decision.reason_text,
        )
        log.info(
            "%s %s score=%d/5 price=%d", decision.signal.value, code,
            decision.score_count, signal.entry_price,
        )
        await self._bus.publish(TOPIC_ENTRY, signal)
        return signal

    async def run_once(
        self,
        symbols: list[str],
        *,
        direction: Direction = Direction.LONG,
    ) -> list[EntrySignal]:
        """후보 종목 리스트를 순차 평가. 개별 종목 실패는 격리."""
        results: list[EntrySignal] = []
        for code in symbols:
            try:
                sig = await self.analyze_symbol(code, direction=direction)
            except Exception:
                log.exception("analyze_symbol failed: %s", code)
                continue
            if sig is not None:
                results.append(sig)
        return results
