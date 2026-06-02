"""core.kis_client.agent_api — /api/agent/* 통합 클라이언트 (CLAUDE.md §22)."""
from __future__ import annotations

import json

import httpx
import pytest

from core.kis_client import AgentApiClient, KisClientConfig, Mode
from core.kis_client.exceptions import KisAuthError, KisBusinessError


def _cfg(agent_key: str = "test-key") -> KisClientConfig:
    return KisClientConfig(
        base_url="http://traidair.test",
        app_key="x" * 20, app_secret="y" * 40, account="12345678-01",
        mode=Mode.PAPER, agent_key=agent_key,
    )


def _client(handler, *, agent_key: str = "test-key") -> AgentApiClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://traidair.test", transport=transport,
        headers={"X-Agent-Key": agent_key},
    )
    return AgentApiClient(_cfg(agent_key), http_client=http)


async def test_screen_candidates_sends_agent_key_and_parses() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["key"] = req.headers.get("X-Agent-Key")
        seen["market"] = req.url.params.get("market")
        return httpx.Response(200, json={
            "ok": True, "market": "0001", "count": 1,
            "candidates": [{"code": "005930", "name": "삼성전자", "turnover": 9e11}],
        })

    async with _client(handler) as c:
        out = await c.screen_candidates(market="kospi", limit=10)
    assert seen["path"] == "/api/agent/screen/candidates"
    assert seen["key"] == "test-key"
    assert seen["market"] == "kospi"
    assert out["candidates"][0]["code"] == "005930"


async def test_quote_indicators_route() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/agent/quote/000660/indicators"
        assert req.url.params.get("tf") == "1"
        return httpx.Response(200, json={
            "ok": True, "code": "000660", "tf": "1",
            "indicators": {"rsi": 55.2, "macd": {"macd": 1.0, "signal": 0.5, "hist": 0.5},
                           "ma5": 100, "ma20": 99, "ma60": 98, "volumeRatio": 2.1, "lastClose": 101},
        })

    async with _client(handler) as c:
        out = await c.quote_indicators("000660", tf="1")
    assert out["indicators"]["rsi"] == 55.2
    assert out["indicators"]["volumeRatio"] == 2.1


async def test_order_posts_body() -> None:
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "POST"
        assert req.url.path == "/api/agent/order"
        captured.update(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "ordNo": "0001", "msg": "주문 완료"})

    async with _client(handler) as c:
        out = await c.order(side="buy", code="005930", qty=10, price=70000, credit=False)
    assert out["ordNo"] == "0001"
    assert captured["side"] == "buy" and captured["code"] == "005930" and captured["qty"] == 10


async def test_journal_append_and_today() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            assert req.url.path == "/api/agent/journal"
            return httpx.Response(200, json={"ok": True, "date": "20260530"})
        assert req.url.path == "/api/agent/journal/today"
        return httpx.Response(200, json={"ok": True, "count": 1, "entries": [{"topic": "x"}]})

    async with _client(handler) as c:
        assert (await c.journal_append({"topic": "signal.entry"}))["ok"] is True
        today = await c.journal_today(limit=50)
    assert today["count"] == 1


async def test_backtest_run_route() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/agent/backtest/run"
        body = json.loads(req.content)
        assert body["days"] == 5
        return httpx.Response(200, json={"ok": True, "state": "started", "pid": 123})

    async with _client(handler) as c:
        out = await c.backtest_run(days=5)
    assert out["state"] == "started"


async def test_ok_false_raises_business_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "토큰 없음"})

    async with _client(handler) as c:
        with pytest.raises(KisBusinessError):
            await c.market_snapshot()


async def test_401_raises_auth_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "error": "unauthorized"})

    async with _client(handler) as c:
        with pytest.raises(KisAuthError):
            await c.positions()
