"""Unit tests for RiskAgent."""
from __future__ import annotations

from datetime import datetime, time
from typing import Callable

import httpx

from agents.analysis.signal.indicators import KST, Direction, Signal
from agents.analysis.signal.main import EntrySignal
from agents.risk.risk_manager.hard_limits import (
    BlackoutWindow,
    HardLimitGate,
    HardLimitsConfig,
)
from agents.risk.risk_manager.main import (
    TOPIC_APPROVED,
    TOPIC_REJECTED,
    ApprovedOrder,
    RejectedOrder,
    RiskAgent,
    SizingParams,
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
        base_url="http://traidair.test",
        transport=transport,
        timeout=httpx.Timeout(6.0),
    )
    cfg = KisClientConfig(
        base_url="http://traidair.test",
        app_key="AK", app_secret="AS",
        account="12345678-01", mode=Mode.PAPER,
    )
    return KisClient(cfg, http_client=http)


def _signal(*, price: int = 70_000, kind: Signal = Signal.STRONG_ENTRY,
            use_credit: bool = False) -> EntrySignal:
    return EntrySignal(
        symbol="005930",
        direction=Direction.LONG,
        signal=kind,
        score_count=4,
        entry_price=price,
        entry_candle_low=price - 500,
        entry_candle_high=price + 200,
        use_credit_hint=use_credit,
        timestamp=datetime(2026, 5, 29, 10, 30, tzinfo=KST),
        reason="STRONG",
    )


_BALANCE_RESPONSE = {
    "ok": True, "cash": 100_000_000, "totalEval": 100_000_000,
    "totalPnl": 0, "positions": [],
}
_ORDERBOOK_RESPONSE = {
    "ok": True,
    "asks": [{"price": 70_000, "qty": 100}],
    "bids": [{"price": 69_950, "qty": 100}],
    "totalAsk": 100, "totalBid": 100, "strength": 100.0,
}


def _midday_clock() -> datetime:
    return datetime(2026, 5, 29, 10, 30, tzinfo=KST)


def _afternoon_clock() -> datetime:
    return datetime(2026, 5, 29, 14, 35, tzinfo=KST)


# ─────────────────────────── happy path ───────────────────────────


async def test_risk_agent_approves_within_limits() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/kis/balance":
            return httpx.Response(200, json=_BALANCE_RESPONSE)
        if req.url.path == "/api/kis/orderbook":
            return httpx.Response(200, json=_ORDERBOOK_RESPONSE)
        raise AssertionError(req.url.path)

    bus = Bus()
    approved = bus.collector(TOPIC_APPROVED)
    rejected = bus.collector(TOPIC_REJECTED)

    async with _kis(handler) as kc:
        agent = RiskAgent(kc, HardLimitGate(_hl()), bus, clock=_midday_clock)
        result = await agent.review(_signal())

    assert isinstance(result, ApprovedOrder)
    assert result.qty > 0
    # cash 1억 × 30% / 70_000 = 428주
    assert result.qty == 100_000_000 * 30 // 100 // 70_000
    assert result.price == 70_000
    assert len(approved) == 1
    assert rejected == []


# ─────────────────────────── reject paths ───────────────────────────


async def test_risk_agent_rejects_blackout() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/kis/balance":
            return httpx.Response(200, json=_BALANCE_RESPONSE)
        if req.url.path == "/api/kis/orderbook":
            return httpx.Response(200, json=_ORDERBOOK_RESPONSE)
        raise AssertionError(req.url.path)

    bus = Bus()
    approved = bus.collector(TOPIC_APPROVED)
    rejected = bus.collector(TOPIC_REJECTED)

    async with _kis(handler) as kc:
        agent = RiskAgent(kc, HardLimitGate(_hl()), bus, clock=_afternoon_clock)
        result = await agent.review(_signal())

    assert isinstance(result, RejectedOrder)
    rule_ids = {v.rule_id for v in result.violations}
    assert "HL-03" in rule_ids
    assert len(rejected) == 1
    assert approved == []


async def test_risk_agent_rejects_short_direction() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no KIS call should occur for short direction")

    bus = Bus()
    rejected = bus.collector(TOPIC_REJECTED)
    signal = EntrySignal(
        symbol="005930",
        direction=Direction.SHORT,
        signal=Signal.STRONG_ENTRY,
        score_count=4,
        entry_price=70_000,
        entry_candle_low=69_500,
        entry_candle_high=70_200,
        use_credit_hint=False,
        timestamp=_midday_clock(),
        reason="STRONG",
    )

    async with _kis(handler) as kc:
        agent = RiskAgent(kc, HardLimitGate(_hl()), bus, clock=_midday_clock)
        result = await agent.review(signal)

    assert isinstance(result, RejectedOrder)
    assert result.violations[0].rule_id == "UNSUPPORTED"
    assert len(rejected) == 1


async def test_risk_agent_rejects_when_cash_too_small() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/kis/balance":
            return httpx.Response(200, json={
                "ok": True, "cash": 100, "totalEval": 100,
                "totalPnl": 0, "positions": [],
            })
        if req.url.path == "/api/kis/orderbook":
            return httpx.Response(200, json=_ORDERBOOK_RESPONSE)
        raise AssertionError(req.url.path)

    bus = Bus()
    rejected = bus.collector(TOPIC_REJECTED)

    async with _kis(handler) as kc:
        agent = RiskAgent(kc, HardLimitGate(_hl()), bus, clock=_midday_clock)
        result = await agent.review(_signal())

    assert isinstance(result, RejectedOrder)
    assert result.violations[0].rule_id == "SIZING"
    assert len(rejected) == 1


# ─────────────────────────── sizing ───────────────────────────


async def test_conditional_entry_uses_smaller_size() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/kis/balance":
            return httpx.Response(200, json=_BALANCE_RESPONSE)
        if req.url.path == "/api/kis/orderbook":
            return httpx.Response(200, json=_ORDERBOOK_RESPONSE)
        raise AssertionError(req.url.path)

    bus = Bus()
    async with _kis(handler) as kc:
        agent = RiskAgent(
            kc, HardLimitGate(_hl()), bus,
            sizing=SizingParams(base_pct_strong=0.30, base_pct_conditional=0.20),
            clock=_midday_clock,
        )
        result = await agent.review(_signal(kind=Signal.CONDITIONAL_ENTRY))
    assert isinstance(result, ApprovedOrder)
    # cash 1억 × 20% / 70_000 = 285주
    assert result.qty == 100_000_000 * 20 // 100 // 70_000
