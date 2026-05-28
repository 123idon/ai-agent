"""Unit tests for OrderAgent."""
from __future__ import annotations

from datetime import datetime
from typing import Callable

import httpx

from agents.analysis.signal.indicators import KST, Direction, Signal
from agents.analysis.signal.main import EntrySignal
from agents.execution.order.main import (
    TOPIC_EVENT,
    TOPIC_FAILED,
    OrderAgent,
    OrderEvent,
    OrderFailed,
)
from agents.risk.risk_manager.main import ApprovedOrder
from core.kis_client import KisClient, KisClientConfig, Mode, OrderType, Side
from core.messaging import Bus


def _kis(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    mode: Mode = Mode.PAPER,
) -> KisClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://traidair.test",
        transport=transport,
        timeout=httpx.Timeout(6.0),
    )
    cfg = KisClientConfig(
        base_url="http://traidair.test",
        app_key="AK", app_secret="AS",
        account="12345678-01", mode=mode,
    )
    return KisClient(cfg, http_client=http)


def _approved(*, use_credit: bool) -> ApprovedOrder:
    sig = EntrySignal(
        symbol="005930",
        direction=Direction.LONG,
        signal=Signal.STRONG_ENTRY,
        score_count=4,
        entry_price=70_000,
        entry_candle_low=69_500,
        entry_candle_high=70_200,
        use_credit_hint=use_credit,
        timestamp=datetime(2026, 5, 29, 10, 30, tzinfo=KST),
        reason="STRONG",
    )
    return ApprovedOrder(
        symbol="005930",
        side=Side.BUY,
        code="005930",
        qty=100,
        price=70_000,
        order_type=OrderType.LIMIT,
        use_credit=use_credit,
        is_new_entry=True,
        entry_signal=sig,
        timestamp=datetime(2026, 5, 29, 10, 30, tzinfo=KST),
        reason="STRONG",
    )


async def test_executes_cash_order_and_publishes_event() -> None:
    paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(req.url.path)
        return httpx.Response(200, json={
            "ok": True, "ordNo": "0001", "msg": "주문 완료",
        })

    bus = Bus()
    events = bus.collector(TOPIC_EVENT)

    async with _kis(handler) as kc:
        agent = OrderAgent(kc, bus)
        result = await agent.execute(_approved(use_credit=False))

    assert isinstance(result, OrderEvent)
    assert result.ord_no == "0001"
    assert "/api/kis/order" in paths
    assert "/api/kis/order-credit" not in paths
    assert len(events) == 1
    assert events[0] == result


async def test_routes_to_credit_when_use_credit_in_live() -> None:
    paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(req.url.path)
        return httpx.Response(200, json={
            "ok": True, "ordNo": "0002", "krxFwdgOrgno": "01234",
            "ordTime": "100000", "msg": "신용주문 완료",
        })

    bus = Bus()
    events = bus.collector(TOPIC_EVENT)

    async with _kis(handler, mode=Mode.LIVE) as kc:
        agent = OrderAgent(kc, bus)
        result = await agent.execute(_approved(use_credit=True))

    assert isinstance(result, OrderEvent)
    assert "/api/kis/order-credit" in paths
    assert "/api/kis/order" not in paths
    assert len(events) == 1


async def test_business_error_emits_failed_event() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "잔고 부족"})

    bus = Bus()
    events = bus.collector(TOPIC_EVENT)
    failed = bus.collector(TOPIC_FAILED)

    async with _kis(handler) as kc:
        agent = OrderAgent(kc, bus)
        result = await agent.execute(_approved(use_credit=False))

    assert isinstance(result, OrderFailed)
    assert "잔고 부족" in result.error
    assert events == []
    assert len(failed) == 1


# ─────────────────────────── end-to-end pipeline ───────────────────────────


async def test_pipeline_signal_to_risk_to_order_via_bus() -> None:
    """분석부 → 리스크부 → 실행부 흐름을 Bus 구독으로 연결."""
    from agents.analysis.signal.main import TOPIC_ENTRY
    from agents.risk.risk_manager.hard_limits import (
        BlackoutWindow, HardLimitGate, HardLimitsConfig,
    )
    from agents.risk.risk_manager.main import TOPIC_APPROVED, RiskAgent
    from datetime import time

    paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(req.url.path)
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
        if req.url.path == "/api/kis/order":
            return httpx.Response(200, json={
                "ok": True, "ordNo": "PIPE-1", "msg": "ok",
            })
        raise AssertionError(req.url.path)

    bus = Bus()
    order_events = bus.collector(TOPIC_EVENT)

    hl = HardLimitsConfig(
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

    async with _kis(handler) as kc:
        risk = RiskAgent(
            kc, HardLimitGate(hl), bus,
            clock=lambda: datetime(2026, 5, 29, 10, 30, tzinfo=KST),
        )
        order = OrderAgent(kc, bus)

        # 분석부의 EntrySignal 도착 시 → 리스크부가 review
        async def on_entry(sig: EntrySignal) -> None:
            await risk.review(sig)

        # 리스크부 APPROVED → 실행부 execute
        async def on_approved(approved: ApprovedOrder) -> None:
            await order.execute(approved)

        bus.subscribe(TOPIC_ENTRY, on_entry)
        bus.subscribe(TOPIC_APPROVED, on_approved)

        # 분석부 모사: 직접 EntrySignal publish
        signal = EntrySignal(
            symbol="005930", direction=Direction.LONG,
            signal=Signal.STRONG_ENTRY, score_count=4,
            entry_price=70_000, entry_candle_low=69_500, entry_candle_high=70_200,
            use_credit_hint=False,
            timestamp=datetime(2026, 5, 29, 10, 30, tzinfo=KST),
            reason="STRONG",
        )
        await bus.publish(TOPIC_ENTRY, signal)

    assert len(order_events) == 1
    assert order_events[0].ord_no == "PIPE-1"
    assert "/api/kis/order" in paths
