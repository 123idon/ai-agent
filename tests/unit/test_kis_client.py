"""Unit tests for KisClient using httpx.MockTransport.

실제 traidair 서버 없이 응답 시나리오만 검증한다.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from core.kis_client import (
    CancelAction,
    CreditLedger,
    KisBusinessError,
    KisClient,
    KisClientConfig,
    KisModeMismatchError,
    Mode,
    Side,
)


def _config(mode: Mode = Mode.PAPER) -> KisClientConfig:
    return KisClientConfig(
        base_url="http://traidair.test",
        app_key="AK",
        app_secret="AS",
        account="12345678-01",
        mode=mode,
    )


def _client(
    handler,
    *,
    mode: Mode = Mode.PAPER,
    ledger: CreditLedger | None = None,
) -> KisClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://traidair.test",
        transport=transport,
        timeout=httpx.Timeout(6.0),
    )
    return KisClient(_config(mode), credit_ledger=ledger, http_client=http)


# ─────────────────────────── basic happy path ───────────────────────────


async def test_price_success() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/kis/price"
        body = json.loads(req.content)
        assert body["code"] == "005930"
        assert body["mode"] == "mock"  # paper → mock
        assert body["appKey"] == "AK"
        return httpx.Response(200, json={
            "ok": True, "code": "005930", "name": "삼성전자",
            "price": 70000, "open": 69500, "high": 70500,
            "low": 69000, "volume": 1234567, "change": 500, "changePct": "0.72",
        })

    async with _client(handler) as kc:
        snap = await kc.get_price("005930")
        assert snap.price == 70000
        assert snap.name == "삼성전자"


async def test_volume_rank_passthrough() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/kis/volume-rank"
        body = json.loads(req.content)
        assert body["market"] == "0001"
        assert body["rankBy"] == 3
        assert body["topN"] == 5
        return httpx.Response(200, json={
            "ok": True, "market": "0001", "rankBy": "3", "count": 1,
            "items": [{
                "rank": 1, "code": "005930", "name": "삼성전자",
                "price": 70000, "change": 500, "changePct": 0.72,
                "volume": 100, "turnover": 7_000_000_000,
                "volSurgePct": 25.5, "volTurnoverPct": 0.12,
            }],
        })

    async with _client(handler) as kc:
        resp = await kc.get_volume_rank(market="0001", rank_by=3, top_n=5)
        assert resp.market == "0001"
        assert resp.items[0].turnover == 7_000_000_000


# ─────────────────────────── error handling ───────────────────────────


async def test_business_error_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "필수 정보 누락"})

    async with _client(handler) as kc:
        with pytest.raises(KisBusinessError) as ei:
            await kc.get_price("005930")
        assert "필수 정보 누락" in str(ei.value)
        assert ei.value.route == "/api/kis/price"


async def test_auth_error_triggers_token_reissue() -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path == "/api/kis/token":
            return httpx.Response(200, json={"ok": True, "token": "fresh1234..."})
        # 첫 번째 /api/kis/price 호출은 토큰 만료로 거절
        if calls.count("/api/kis/price") == 1:
            return httpx.Response(200, json={"ok": False, "error": "토큰 없음"})
        return httpx.Response(200, json={
            "ok": True, "code": "005930", "name": "X",
            "price": 1, "open": 1, "high": 1, "low": 1,
            "volume": 0, "change": 0, "changePct": "0.00",
        })

    async with _client(handler) as kc:
        snap = await kc.get_price("005930")
        assert snap.price == 1
        assert calls.count("/api/kis/price") == 2
        assert calls.count("/api/kis/token") == 1


# ─────────────────────────── credit (margin) ───────────────────────────


async def test_credit_order_blocked_in_paper() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with _client(handler, mode=Mode.PAPER) as kc:
        with pytest.raises(KisModeMismatchError):
            await kc.place_credit_order(
                side=Side.BUY, code="005930", qty=10, price=70000,
            )


async def test_credit_buy_records_loan_dt(tmp_path: Path) -> None:
    ledger = CreditLedger(tmp_path / "ledger.json")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True, "ordNo": "0001", "krxFwdgOrgno": "01234",
            "ordTime": "100000", "msg": "ok",
        })

    async with _client(handler, mode=Mode.LIVE, ledger=ledger) as kc:
        result = await kc.place_credit_order(
            side=Side.BUY, code="005930", qty=10, price=70000,
        )
        assert result.ordNo == "0001"
        assert ledger.loan_dt("005930") is not None


async def test_credit_sell_auto_injects_loan_dt(tmp_path: Path) -> None:
    ledger = CreditLedger(tmp_path / "ledger.json")
    ledger.record_buy("005930")
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/kis/order-credit":
            captured.update(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "ordNo": "0002"})

    async with _client(handler, mode=Mode.LIVE, ledger=ledger) as kc:
        await kc.place_credit_order(side=Side.SELL, code="005930", qty=10)

    assert captured.get("loanDate"), "loanDate must be auto-injected from ledger"
    assert captured.get("crdtType", "25") == "25"


async def test_credit_sell_without_ledger_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with _client(handler, mode=Mode.LIVE, ledger=None) as kc:
        with pytest.raises(KisBusinessError):
            await kc.place_credit_order(side=Side.SELL, code="005930", qty=10)


# ─────────────────────────── cancel / unfilled ───────────────────────────


async def test_cancel_order_payload_shape() -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(json.loads(req.content))
        return httpx.Response(200, json={
            "ok": True, "ordNo": "0009", "krxFwdgOrgno": "01234",
            "action": "cancel", "msg": "취소 완료",
        })

    async with _client(handler) as kc:
        result = await kc.cancel_order(
            org_ord_no="0007", krx_fwdg_orgno="01234",
            action=CancelAction.CANCEL, qty=10,
        )
    assert result.action == CancelAction.CANCEL
    assert captured["orgOrdNo"] == "0007"
    assert captured["qtyAllOrd"] == "Y"
    assert captured["action"] == "cancel"


async def test_unfilled_returns_list() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": True, "count": 2, "orders": [
                {
                    "ordNo": "0001", "orgOrdNo": "",
                    "krxFwdgOrgno": "01234", "code": "005930", "name": "삼성전자",
                    "side": "buy", "ordQty": 10, "filledQty": 3, "unfilledQty": 7,
                    "ordPrice": 70000, "ordTime": "100000",
                    "ordDvsn": "00", "ordDvsnName": "지정가",
                },
                {
                    "ordNo": "0002", "orgOrdNo": "",
                    "krxFwdgOrgno": "01234", "code": "000660", "name": "SK하이닉스",
                    "side": "sell", "ordQty": 5, "filledQty": 0, "unfilledQty": 5,
                    "ordPrice": 150000, "ordTime": "100500",
                    "ordDvsn": "00", "ordDvsnName": "지정가",
                },
            ],
        })

    async with _client(handler) as kc:
        orders = await kc.get_unfilled()
    assert len(orders) == 2
    assert orders[0].side == Side.BUY
    assert orders[1].unfilledQty == 5


# ─────────────────────────── market-data (paper-only sim) ───────────────────────────


async def test_market_data_sim_blocked_in_live() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with _client(handler, mode=Mode.LIVE) as kc:
        with pytest.raises(KisModeMismatchError):
            await kc.get_market_data(mode="sim", date="2026-05-01", time="10:00")


async def test_market_data_realtime_parses() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/market-data"
        assert req.url.params.get("mode") == "realtime"
        return httpx.Response(200, json={
            "mode": "realtime",
            "fetchedAt": "2026-05-29T01:00:00Z",
            "cutoffKST": "2026-05-29 10:00:00 KST",
            "indices": {
                "kospi": {"price": 2700.5, "prev": 2690.0, "chgPct": 0.39,
                          "lastUpdated": None, "lastUpdatedKST": None},
                "vix":   {"price": 14.2, "prev": 14.5, "chgPct": -2.07,
                          "lastUpdated": None, "lastUpdatedKST": None},
            },
        })

    async with _client(handler) as kc:
        md = await kc.get_market_data(mode="realtime")
    assert md.indices["kospi"].price == 2700.5
    assert md.indices["vix"].chgPct == -2.07
