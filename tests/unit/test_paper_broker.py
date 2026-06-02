"""Unit tests for PaperBroker + KisClient 모의 주문 시뮬레이션 (CLAUDE.md §3.1)."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from core.kis_client import KisClient, KisClientConfig, Mode, PaperBroker, Side


# ─────────────────────────── PaperBroker ───────────────────────────


def test_buy_then_sell_cash_and_position() -> None:
    b = PaperBroker(persist_path=None, start_cash=1_000_000)
    b.fill(Side.BUY, "005930", 2, 100_000)
    assert b.cash == 800_000.0
    assert b.positions["005930"].qty == 2
    b.fill(Side.SELL, "005930", 1, 110_000)
    assert b.cash == 910_000.0
    assert b.positions["005930"].qty == 1
    assert b.orderable_cash() == 910_000


def test_average_price_on_add() -> None:
    b = PaperBroker(persist_path=None, start_cash=10_000_000)
    b.fill(Side.BUY, "AAA", 1, 100)
    b.fill(Side.BUY, "AAA", 1, 200)
    assert b.positions["AAA"].avg_price == 150.0


def test_sell_all_removes_position() -> None:
    b = PaperBroker(persist_path=None, start_cash=1_000_000)
    b.fill(Side.BUY, "AAA", 3, 1000)
    b.fill(Side.SELL, "AAA", 5, 1100)   # 보유보다 많이 → 보유분만 청산
    assert "AAA" not in b.positions


def test_persistence_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "paper_broker.json"
    b = PaperBroker(persist_path=p, start_cash=5_000_000)
    b.fill(Side.BUY, "005930", 10, 70_000)
    assert p.exists()
    # 새 인스턴스가 디스크 상태를 복원
    b2 = PaperBroker(persist_path=p, start_cash=5_000_000)
    assert b2.cash == b.cash
    assert b2.positions["005930"].qty == 10
    assert b2.positions["005930"].avg_price == 70_000.0


# ─────────────────────────── KisClient 시뮬 라우팅 ───────────────────────────


def _sim_config() -> KisClientConfig:
    return KisClientConfig(
        base_url="http://traidair.test", app_key="AK", app_secret="AS",
        account="12345678-01", mode=Mode.PAPER,
        simulate_orders=True, paper_start_cash=1_000_000,
    )


def _sim_client(handler, broker: PaperBroker) -> KisClient:
    http = httpx.AsyncClient(
        base_url="http://traidair.test",
        transport=httpx.MockTransport(handler), timeout=httpx.Timeout(6.0),
    )
    return KisClient(_sim_config(), paper_broker=broker, http_client=http)


async def test_paper_order_does_not_hit_traidair() -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        return httpx.Response(200, json={"ok": False, "error": "should not be called"})

    broker = PaperBroker(persist_path=None, start_cash=1_000_000)
    async with _sim_client(handler, broker) as kc:
        res = await kc.place_order(side=Side.BUY, code="005930", qty=2, price=100_000)
        assert res.ordNo.startswith("PAPER")
        assert "/api/kis/order" not in calls       # 실주문 미발생
        assert broker.cash == 800_000.0


async def test_paper_market_order_uses_real_price() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/kis/price"     # 시장가 → 실전 시세 조회
        return httpx.Response(200, json={
            "ok": True, "code": "005930", "name": "삼성전자", "price": 70000,
            "open": 0, "high": 0, "low": 0, "volume": 0, "change": 0, "changePct": "0",
        })

    broker = PaperBroker(persist_path=None, start_cash=1_000_000)
    async with _sim_client(handler, broker) as kc:
        res = await kc.place_order(side=Side.BUY, code="005930", qty=10, price=0)
        assert "@70000" in (res.msg or "")
        assert broker.positions["005930"].avg_price == 70000.0


async def test_paper_balance_from_broker_with_real_price() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        # 잔고 평가는 /api/kis/balance 가 아니라 실전 /api/kis/price 로 계산
        assert req.url.path == "/api/kis/price"
        return httpx.Response(200, json={
            "ok": True, "code": "005930", "name": "삼성전자", "price": 80000,
            "open": 0, "high": 0, "low": 0, "volume": 0, "change": 0, "changePct": "0",
        })

    broker = PaperBroker(persist_path=None, start_cash=1_000_000)
    broker.fill(Side.BUY, "005930", 5, 70_000)   # cash 650,000
    async with _sim_client(handler, broker) as kc:
        bal = await kc.get_balance()
        assert bal.cash == 650_000
        pos = bal.positions[0]
        assert pos.code == "005930" and pos.qty == 5
        assert pos.currentPrice == 80000
        assert pos.pnl == (80000 - 70000) * 5      # 평가손익 = 실시세 기반
        assert bal.totalEval == 650_000 + 80000 * 5


async def test_paper_orderable_from_cash() -> None:
    broker = PaperBroker(persist_path=None, start_cash=1_000_000)
    async with _sim_client(lambda r: httpx.Response(200, json={"ok": True}), broker) as kc:
        oa = await kc.get_orderable_amount(code="005930", price=100_000)
        assert oa.orderCashable == 1_000_000
        assert oa.maxBuyAmt == 1_000_000
        assert oa.maxBuyQty == 10


async def test_paper_unfilled_empty() -> None:
    broker = PaperBroker(persist_path=None, start_cash=1_000_000)
    async with _sim_client(lambda r: httpx.Response(200, json={"ok": True}), broker) as kc:
        assert await kc.get_unfilled() == []
