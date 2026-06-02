"""Signal analysis agent (CLAUDE.md §2.3).

분봉 캔들을 받아 5지표를 평가하고, ``Signal.NO_ENTRY``가 아니면
``signal.entry`` 토픽으로 ``EntrySignal``을 발행한다.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from core.kis_client import KisClient
from core.messaging import Bus
from core.notion_client import NotionKnowledgeView

from .indicators import KST, Direction, Signal, SignalAnalyzer

log = logging.getLogger(__name__)

TOPIC_ENTRY = "signal.entry"
TOPIC_ANALYSIS = "signal.analysis"   # 5지표 상세 (진입/미진입 모두 — 에이전트 모니터용)


def _downgrade(sig: "Signal") -> "Signal":
    """신뢰도 하향 — 진입 강도를 한 단계 낮춘다(§19 메모리)."""
    if sig == Signal.STRONG_ENTRY:
        return Signal.CONDITIONAL_ENTRY
    return Signal.NO_ENTRY


@dataclass(frozen=True)
class IndicatorView:
    name: str           # volume | rsi | macd | ma | candle
    passed: bool
    detail: str
    value: float | None


@dataclass(frozen=True)
class SignalAnalysis:
    """신호분석 전체 결과 (NO_ENTRY 포함). 에이전트 사고과정 모니터링용(§2 대시보드)."""

    symbol: str
    signal: Signal
    score_count: int
    indicators: tuple[IndicatorView, ...]
    reason: str
    timestamp: datetime
    confidence: float | None = None       # §19 이 패턴의 과거 승률(%)
    confidence_trades: int = 0            # 표본 수


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
    atr_pct: float | None = None  # §5.3 ATR 기반 익절 목표가 동적 산출용
    daily_strong: bool = False    # 일봉 게이트 통과(강세) → 리스크부 사이즈 ↑ (§5 사이징)


class SignalAgent:
    def __init__(
        self,
        kis: KisClient,
        analyzer: SignalAnalyzer,
        bus: Bus,
        *,
        chart_tf: str = "5",
        use_credit_default: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(KST),
        pattern_memory: Callable[[str, list[str]], tuple[float | None, int]] | None = None,
        confidence_floor: float = 35.0,
        confidence_min_trades: int = 5,
        notion_knowledge: NotionKnowledgeView | None = None,
    ) -> None:
        self._kis = kis
        self._analyzer = analyzer
        self._bus = bus
        self._chart_tf = chart_tf
        self._use_credit_default = use_credit_default
        self._clock = clock
        # §19 메모리: 패턴(신호강도+통과지표) 과거 승률이 낮으면 진입 보류(신뢰도 하향).
        self._pattern_memory = pattern_memory
        self._conf_floor = confidence_floor
        self._conf_min = confidence_min_trades
        # 학습부 노션 지식(세션 시작 시 참조) — 매수 진입 조건 카테고리.
        self._notion = notion_knowledge
        if notion_knowledge is not None and notion_knowledge.available:
            note = notion_knowledge.summary_line("analysis.signal")
            if note:
                log.info("📚 신호분석 %s", note)

    async def analyze_symbol(
        self,
        code: str,
        *,
        direction: Direction = Direction.LONG,
    ) -> EntrySignal | None:
        chart = await self._kis.get_chart(code, tf=self._chart_tf)
        if not chart.candles:
            log.debug("no candles for %s", code)
            return None

        # 일봉 게이트용 일봉 캔들(있으면). 미지원/실패 시 None → 분봉만으로 판정(§5.2).
        daily = await self._daily_candles(code)

        decision = self._analyzer.evaluate(
            code, chart.candles, direction=direction, now=self._clock(),
            daily_candles=daily,
        )
        passed = [s.name for s in decision.scores if s.passed]

        # §19 메모리: 이 패턴의 과거 승률 조회 → 낮으면 진입 보류(신뢰도 하향).
        conf: float | None = None
        conf_n = 0
        effective = decision.signal
        reason_text = decision.reason_text
        if self._pattern_memory is not None and decision.signal != Signal.NO_ENTRY:
            conf, conf_n = self._pattern_memory(decision.signal.value, passed)
            if conf is not None and conf_n >= self._conf_min and conf < self._conf_floor:
                # 신뢰도 하향: 한 단계 강등 (STRONG→CONDITIONAL→NO_ENTRY). 완전 차단이 아니라
                # 보수화 — 과거 잘 안 된 패턴은 비중을 줄여 진입(또는 보류)한다.
                effective = _downgrade(decision.signal)
                reason_text += f" | 신뢰도 하향(과거 승률 {conf:.0f}%·{conf_n}회)"

        # 5지표 상세를 항상 발행 (진입/미진입 모두 — 에이전트 모니터)
        await self._bus.publish(TOPIC_ANALYSIS, SignalAnalysis(
            symbol=code,
            signal=effective,
            score_count=decision.score_count,
            indicators=tuple(
                IndicatorView(name=s.name, passed=s.passed, detail=s.detail, value=s.value)
                for s in decision.scores
            ),
            reason=reason_text,
            timestamp=decision.timestamp,
            confidence=conf,
            confidence_trades=conf_n,
        ))
        if effective == Signal.NO_ENTRY:
            # 매 분·매 후보마다 발생하는 핫 로그 → DEBUG(콘솔 I/O 절감, 요구 3d).
            # 모니터링은 위 signal.analysis 토픽으로 발행되므로 영향 없음.
            log.debug("NO_ENTRY %s: %s", code, reason_text)
            return None

        last = chart.candles[-1]
        signal = EntrySignal(
            symbol=code,
            direction=decision.direction,
            signal=effective,
            score_count=decision.score_count,
            entry_price=int(last.c),
            entry_candle_low=decision.entry_candle_low,
            entry_candle_high=decision.entry_candle_high,
            use_credit_hint=self._use_credit_default,
            timestamp=decision.timestamp,
            reason=decision.reason_text,
            atr_pct=decision.atr_pct,
            daily_strong=decision.daily_strong,
        )
        log.info(
            "%s %s score=%d/5 price=%d", decision.signal.value, code,
            decision.score_count, signal.entry_price,
        )
        await self._bus.publish(TOPIC_ENTRY, signal)
        return signal

    async def _daily_candles(self, code: str):
        """일봉 게이트용 일봉 캔들 조회. 클라이언트가 ``get_daily_chart`` 를 제공하면 사용.

        live(KisClient)·backtest(ReplayKisClient) 모두 best-effort — 미지원/실패 시
        None을 돌려주면 분석부가 분봉만으로 판정한다(§5.2, 무거래 자가정지 방지 §19).
        """
        getter = getattr(self._kis, "get_daily_chart", None)
        if getter is None:
            return None
        try:
            chart = await getter(code)
        except Exception:  # noqa: BLE001
            log.debug("daily chart fetch failed: %s", code, exc_info=True)
            return None
        return chart.candles or None

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
