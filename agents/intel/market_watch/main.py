"""Market-watch agent (CLAUDE.md §2.2.2).

매크로 지수(KOSPI/KOSDAQ/VIX/USD-KRW 등)를 폴링하여
GREEN/YELLOW/RED/BLACK 등급을 산출하고 ``market.state`` 토픽으로 발행한다.
RiskAgent는 등급을 참조하여 신규 진입을 차단할 수 있다.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from agents.analysis.signal.indicators import KST
from core.kis_client import KisClient, MacroIndex
from core.messaging import Bus
from core.notion_client import NotionKnowledgeView

log = logging.getLogger(__name__)

TOPIC_STATE = "market.state"


class MarketGrade(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    BLACK = "BLACK"


@dataclass(frozen=True)
class MarketState:
    grade: MarketGrade
    timestamp: datetime
    kospi_chg_pct: float | None
    kosdaq_chg_pct: float | None
    vix: float | None
    usdkrw_chg_pct: float | None
    reason: str


@dataclass(frozen=True)
class MarketWatchParams:
    yellow_kospi_chg_pct: float = -0.8
    red_kospi_chg_pct: float = -1.5
    black_kospi_chg_pct: float = -3.0
    yellow_vix: float = 20.0
    red_vix: float = 25.0
    black_vix: float = 35.0
    usdkrw_yellow_abs_pct: float = 1.0
    poll_seconds: int = 60


class MarketWatchAgent:
    def __init__(
        self,
        kis: KisClient,
        bus: Bus,
        params: MarketWatchParams | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(KST),
        notion_knowledge: NotionKnowledgeView | None = None,
    ) -> None:
        self._kis = kis
        self._bus = bus
        self._p = params or MarketWatchParams()
        self._clock = clock
        # 학습부 노션 지식(세션 시작 시 참조) — 시간대별·시장 환경 전략 카테고리.
        self._notion = notion_knowledge
        if notion_knowledge is not None and notion_knowledge.available:
            note = notion_knowledge.summary_line("intel.market_watch")
            if note:
                log.info("📚 시장상황 %s", note)

    async def poll_once(self) -> MarketState:
        md = await self._kis.get_market_data(mode="realtime")
        kospi = md.indices.get("kospi")
        kosdaq = md.indices.get("kosdq")     # server.js uses "kosdq"
        vix = md.indices.get("vix")
        usdkrw = md.indices.get("usdkrw")

        grade, reason = self._grade(kospi, vix, usdkrw)
        state = MarketState(
            grade=grade,
            timestamp=self._clock(),
            kospi_chg_pct=kospi.chgPct if kospi else None,
            kosdaq_chg_pct=kosdaq.chgPct if kosdaq else None,
            vix=vix.price if vix else None,
            usdkrw_chg_pct=usdkrw.chgPct if usdkrw else None,
            reason=reason,
        )
        log.info("market.state = %s (%s)", grade.value, reason)
        await self._bus.publish(TOPIC_STATE, state)
        return state

    def _grade(
        self,
        kospi: MacroIndex | None,
        vix: MacroIndex | None,
        usdkrw: MacroIndex | None,
    ) -> tuple[MarketGrade, str]:
        kospi_chg = kospi.chgPct if kospi else None
        vix_price = vix.price if vix else None
        usd_chg = usdkrw.chgPct if usdkrw else None

        # BLACK (즉시 종료)
        if kospi_chg is not None and kospi_chg <= self._p.black_kospi_chg_pct:
            return MarketGrade.BLACK, f"KOSPI {kospi_chg:.2f}% ≤ {self._p.black_kospi_chg_pct}%"
        if vix_price is not None and vix_price >= self._p.black_vix:
            return MarketGrade.BLACK, f"VIX {vix_price:.1f} ≥ {self._p.black_vix}"

        # RED
        reasons: list[str] = []
        if kospi_chg is not None and kospi_chg <= self._p.red_kospi_chg_pct:
            reasons.append(f"KOSPI {kospi_chg:.2f}% ≤ {self._p.red_kospi_chg_pct}%")
        if vix_price is not None and vix_price >= self._p.red_vix:
            reasons.append(f"VIX {vix_price:.1f} ≥ {self._p.red_vix}")
        if reasons:
            return MarketGrade.RED, ", ".join(reasons)

        # YELLOW
        if kospi_chg is not None and kospi_chg <= self._p.yellow_kospi_chg_pct:
            reasons.append(f"KOSPI {kospi_chg:.2f}% ≤ {self._p.yellow_kospi_chg_pct}%")
        if vix_price is not None and vix_price >= self._p.yellow_vix:
            reasons.append(f"VIX {vix_price:.1f} ≥ {self._p.yellow_vix}")
        if usd_chg is not None and abs(usd_chg) >= self._p.usdkrw_yellow_abs_pct:
            reasons.append(f"USDKRW |{usd_chg:.2f}%| ≥ {self._p.usdkrw_yellow_abs_pct}%")
        if reasons:
            return MarketGrade.YELLOW, ", ".join(reasons)
        return MarketGrade.GREEN, "정상"

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.poll_once()
            except Exception:
                log.exception("market_watch poll failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._p.poll_seconds)
            except asyncio.TimeoutError:
                continue
