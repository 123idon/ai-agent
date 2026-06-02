"""Unit tests for SignalAgent."""
from __future__ import annotations

import json
from typing import Callable

import httpx

from agents.analysis.signal.indicators import (
    Direction,
    Signal,
    SignalAnalyzer,
    SignalParams,
)
from agents.analysis.signal.main import TOPIC_ENTRY, EntrySignal, SignalAgent
from core.kis_client import KisClient, KisClientConfig, Mode
from core.messaging import Bus


def _params() -> SignalParams:
    return SignalParams(
        volume_surge_multiplier=2.0,
        rsi_period=14,
        rsi_oversold=30.0,
        rsi_overbought=70.0,
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
        ma_periods=(5, 20, 60),
        strong_min_indicators=4,
        conditional_min_indicators=3,
        candle_long=("hammer", "bullish_engulfing", "long_bullish"),
        candle_short=("shooting_star", "bearish_engulfing", "long_bearish"),
    )


def _empty_chart(code: str) -> dict:
    """일봉(tf=D) 요청에 빈 응답 — 본 단위테스트는 분봉 타점만 검증(일봉 미확인)."""
    return {
        "ok": True, "code": code, "date": "20260529",
        "prevDate": "20260528", "tf": "D",
        "candles": [], "prevCount": 0, "todayCount": 0,
    }


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


def _flat_chart(code: str, n: int = 5) -> dict:
    candles = [
        {"t": "10:00", "date": "20260529",
         "o": 100, "h": 100, "l": 100, "c": 100, "v": 100}
        for _ in range(n)
    ]
    return {
        "ok": True, "code": code, "date": "20260529",
        "prevDate": "20260528", "tf": "1",
        "candles": candles, "prevCount": 0, "todayCount": n,
    }


def _signal_chart(code: str) -> dict:
    """80개 캔들: 상승추세 + 마지막 직전 음봉 + 마지막 양봉(거래량 4배, bullish engulfing).

    traidair는 모든 가격 필드를 ``parseInt()``로 정수화하므로 테스트 데이터도 정수만 사용.
    """
    candles: list[dict] = []
    for i in range(78):
        c = 10_000 + 50 * i  # 10_000 → 13_850
        candles.append({
            "t": f"10:{i:02d}", "date": "20260529",
            "o": c - 30, "h": c + 50, "l": c - 40, "c": c, "v": 100,
        })
    candles.append({
        "t": "11:18", "date": "20260529",
        "o": 13_950, "h": 14_000, "l": 13_900, "c": 13_920, "v": 100,   # 음봉
    })
    candles.append({
        "t": "11:19", "date": "20260529",
        "o": 13_900, "h": 14_150, "l": 13_890, "c": 14_140, "v": 400,   # 양봉, engulfing
    })
    return {
        "ok": True, "code": code, "date": "20260529",
        "prevDate": "20260528", "tf": "1",
        "candles": candles, "prevCount": 0, "todayCount": len(candles),
    }


async def test_no_entry_does_not_publish() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        return httpx.Response(200, json=_flat_chart(body["code"]))

    bus = Bus()
    received = bus.collector(TOPIC_ENTRY)
    async with _kis(handler) as kc:
        agent = SignalAgent(kc, SignalAnalyzer(_params()), bus)
        result = await agent.analyze_symbol("005930", direction=Direction.LONG)
    assert result is None
    assert received == []


async def test_publishes_entry_signal_when_triggered() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body.get("tf") == "D":
            return httpx.Response(200, json=_empty_chart(body["code"]))
        return httpx.Response(200, json=_signal_chart(body["code"]))

    bus = Bus()
    received = bus.collector(TOPIC_ENTRY)
    async with _kis(handler) as kc:
        agent = SignalAgent(kc, SignalAnalyzer(_params()), bus)
        result = await agent.analyze_symbol("005930", direction=Direction.LONG)

    assert isinstance(result, EntrySignal)
    assert result.symbol == "005930"
    assert result.signal in (Signal.STRONG_ENTRY, Signal.CONDITIONAL_ENTRY)
    assert result.entry_price == 14_140      # last close
    assert result.entry_candle_low == 13_890
    assert received == [result]


async def test_run_once_isolates_per_symbol_errors() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(req.content)
        if body["code"] == "BAD":
            return httpx.Response(200, json={"ok": False, "error": "no data"})
        if body.get("tf") == "D":
            return httpx.Response(200, json=_empty_chart(body["code"]))
        return httpx.Response(200, json=_signal_chart(body["code"]))

    bus = Bus()
    received = bus.collector(TOPIC_ENTRY)
    async with _kis(handler) as kc:
        agent = SignalAgent(kc, SignalAnalyzer(_params()), bus)
        results = await agent.run_once(["BAD", "005930"])
    assert len(results) == 1
    assert results[0].symbol == "005930"
    assert len(received) == 1
