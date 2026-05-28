"""Unit tests for MarketWatchAgent."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Callable

import httpx

from agents.analysis.signal.indicators import KST
from agents.intel.market_watch.main import (
    TOPIC_STATE,
    MarketGrade,
    MarketState,
    MarketWatchAgent,
    MarketWatchParams,
)
from core.kis_client import KisClient, KisClientConfig, Mode
from core.messaging import Bus


def _kis(handler: Callable[[httpx.Request], httpx.Response]) -> KisClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://traidair.test", transport=transport,
        timeout=httpx.Timeout(6.0),
    )
    cfg = KisClientConfig(
        base_url="http://traidair.test",
        app_key="AK", app_secret="AS",
        account="12345678-01", mode=Mode.PAPER,
    )
    return KisClient(cfg, http_client=http)


def _md_response(**indices) -> dict:
    return {
        "mode": "realtime",
        "fetchedAt": "2026-05-29T01:00:00Z",
        "cutoffKST": "2026-05-29 10:00:00 KST",
        "indices": {
            k: {"price": v[0], "prev": v[1], "chgPct": v[2],
                "lastUpdated": None, "lastUpdatedKST": None}
            for k, v in indices.items()
        },
    }


async def test_green_when_normal() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_md_response(
            kospi=(2700, 2695, 0.18),
            vix=(14.0, 14.5, -3.4),
            usdkrw=(1370, 1369, 0.07),
        ))

    bus = Bus()
    received = bus.collector(TOPIC_STATE)
    async with _kis(handler) as kc:
        result = await MarketWatchAgent(kc, bus).poll_once()
    assert result.grade == MarketGrade.GREEN
    assert isinstance(received[0], MarketState)


async def test_yellow_on_vix_threshold() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_md_response(
            kospi=(2700, 2695, 0.18),
            vix=(22.0, 18.0, 22.2),
            usdkrw=(1370, 1369, 0.07),
        ))
    bus = Bus()
    async with _kis(handler) as kc:
        result = await MarketWatchAgent(kc, bus).poll_once()
    assert result.grade == MarketGrade.YELLOW
    assert "VIX" in result.reason


async def test_red_on_kospi_drop() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_md_response(
            kospi=(2660, 2700, -1.6),
            vix=(15.0, 14.0, 7.1),
            usdkrw=(1370, 1369, 0.07),
        ))
    bus = Bus()
    async with _kis(handler) as kc:
        result = await MarketWatchAgent(kc, bus).poll_once()
    assert result.grade == MarketGrade.RED


async def test_black_on_severe_kospi_drop() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_md_response(
            kospi=(2600, 2700, -3.5),
            vix=(15.0, 14.0, 7.1),
            usdkrw=(1370, 1369, 0.07),
        ))
    bus = Bus()
    async with _kis(handler) as kc:
        result = await MarketWatchAgent(kc, bus).poll_once()
    assert result.grade == MarketGrade.BLACK
