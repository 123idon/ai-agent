"""Risk agent의 market-state 통합 테스트."""
from __future__ import annotations

from datetime import datetime, time
from typing import Callable

import httpx

from agents.analysis.signal.indicators import KST, Direction, Signal
from agents.analysis.signal.main import EntrySignal
from agents.intel.market_watch.main import MarketGrade
from agents.risk.risk_manager.hard_limits import (
    BlackoutWindow, HardLimitGate, HardLimitsConfig,
)
from agents.risk.risk_manager.main import (
    TOPIC_APPROVED,
    TOPIC_REJECTED,
    ApprovedOrder,
    RejectedOrder,
    RiskAgent,
)
from core.kis_client import KisClient, KisClientConfig, Mode
from core.messaging import Bus


def _hl() -> HardLimitsConfig:
    return HardLimitsConfig(
        max_concurrent_positions=3,
        consecutive_stoploss_threshold=3,
        cooldown_after_stoploss_minutes=60,
        entry_blackout_windows=(
            BlackoutWindow(time(9, 0), time(9, 30), "장초반"),
            BlackoutWindow(time(14, 30), time(15, 30), "장후반"),
        ),
        max_slippage_ticks=5,
        margin_maintenance_buffer_pct=0.05,
        version="2.0.0",
    )


def _kis(handler: Callable[[httpx.Request], httpx.Response]) -> KisClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://traidair.test", transport=transport,
        timeout=httpx.Timeout(6.0),
    )
    cfg = KisClientConfig(
        base_url="http://traidair.test", app_key="AK", app_secret="AS",
        account="12345678-01", mode=Mode.PAPER,
    )
    return KisClient(cfg, http_client=http)


def _balance_handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/api/kis/balance":
        return httpx.Response(200, json={
            "ok": True, "cash": 100_000_000, "totalEval": 100_000_000,
            "totalPnl": 0, "positions": [],
        })
    if req.url.path == "/api/kis/orderbook":
        return httpx.Response(200, json={
            "ok": True,
            "asks": [{"price": 70_000, "qty": 100}],
            "bids": [{"price": 69_950, "qty": 100}],
            "totalAsk": 100, "totalBid": 100, "strength": 100.0,
        })
    raise AssertionError(req.url.path)


def _signal(kind: Signal = Signal.STRONG_ENTRY) -> EntrySignal:
    return EntrySignal(
        symbol="005930", direction=Direction.LONG, signal=kind, score_count=4,
        entry_price=70_000, entry_candle_low=69_500, entry_candle_high=70_200,
        use_credit_hint=False,
        timestamp=datetime(2026, 5, 29, 10, 30, tzinfo=KST),
        reason="STRONG",
    )


def _clock() -> datetime:
    return datetime(2026, 5, 29, 10, 30, tzinfo=KST)


async def test_red_market_rejects_strong_entry() -> None:
    bus = Bus()
    rejected = bus.collector(TOPIC_REJECTED)
    async with _kis(_balance_handler) as kc:
        agent = RiskAgent(
            kc, HardLimitGate(_hl()), bus, clock=_clock,
            market_state_provider=lambda: MarketGrade.RED,
        )
        result = await agent.review(_signal())
    assert isinstance(result, RejectedOrder)
    assert any(v.rule_id == "MARKET_RED" for v in result.violations)
    assert len(rejected) == 1


async def test_black_market_rejects() -> None:
    bus = Bus()
    rejected = bus.collector(TOPIC_REJECTED)
    async with _kis(_balance_handler) as kc:
        agent = RiskAgent(
            kc, HardLimitGate(_hl()), bus, clock=_clock,
            market_state_provider=lambda: MarketGrade.BLACK,
        )
        result = await agent.review(_signal())
    assert isinstance(result, RejectedOrder)
    assert any(v.rule_id == "MARKET_BLACK" for v in result.violations)


async def test_yellow_blocks_conditional_but_not_strong() -> None:
    bus = Bus()
    approved = bus.collector(TOPIC_APPROVED)
    rejected = bus.collector(TOPIC_REJECTED)
    async with _kis(_balance_handler) as kc:
        agent = RiskAgent(
            kc, HardLimitGate(_hl()), bus, clock=_clock,
            market_state_provider=lambda: MarketGrade.YELLOW,
        )
        strong = await agent.review(_signal(Signal.STRONG_ENTRY))
        conditional = await agent.review(_signal(Signal.CONDITIONAL_ENTRY))

    assert isinstance(strong, ApprovedOrder)
    assert isinstance(conditional, RejectedOrder)
    assert any(v.rule_id == "MARKET_YELLOW_CONDITIONAL" for v in conditional.violations)
    assert len(approved) == 1
    assert len(rejected) == 1


async def test_green_allows_both() -> None:
    bus = Bus()
    approved = bus.collector(TOPIC_APPROVED)
    async with _kis(_balance_handler) as kc:
        agent = RiskAgent(
            kc, HardLimitGate(_hl()), bus, clock=_clock,
            market_state_provider=lambda: MarketGrade.GREEN,
        )
        await agent.review(_signal(Signal.STRONG_ENTRY))
        await agent.review(_signal(Signal.CONDITIONAL_ENTRY))
    assert len(approved) == 2
