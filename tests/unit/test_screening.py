"""Unit tests for ScreeningAgent."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Callable

import httpx

from agents.analysis.signal.indicators import KST
from agents.intel.screening.main import (
    TOPIC_CANDIDATES,
    ScreeningAgent,
    ScreeningCandidate,
    ScreeningParams,
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
        base_url="http://traidair.test", app_key="AK", app_secret="AS",
        account="12345678-01", mode=Mode.PAPER,
    )
    return KisClient(cfg, http_client=http)


def _make_uptrend_candles(date: str = "20260529", base: int = 10_000) -> list[dict]:
    candles = []
    for i in range(70):
        c = base + 50 * i
        candles.append({
            "t": f"10:{i:02d}", "date": date,
            "o": c - 30, "h": c + 50, "l": c - 40, "c": c, "v": 100,
        })
    return candles


def _make_vol_rank_response() -> dict:
    return {
        "ok": True, "market": "0000", "rankBy": "3", "count": 2,
        "items": [
            {
                "rank": 1, "code": "005930", "name": "삼성전자",
                "price": 70_000, "change": 500, "changePct": 0.72,
                "volume": 1_000_000, "turnover": 70_000_000_000,
                "volSurgePct": 50.0, "volTurnoverPct": 0.5,
            },
            {
                "rank": 2, "code": "000660", "name": "SK하이닉스",
                "price": 150_000, "change": -500, "changePct": -0.33,
                "volume": 500_000, "turnover": 75_000_000_000,
                "volSurgePct": 30.0, "volTurnoverPct": 0.3,
            },
        ],
    }


async def test_screening_publishes_candidates_above_threshold() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/kis/volume-rank":
            return httpx.Response(200, json=_make_vol_rank_response())
        if req.url.path == "/api/kis/chart":
            body = json.loads(req.content)
            return httpx.Response(200, json={
                "ok": True, "code": body["code"], "date": "20260529",
                "prevDate": "20260528", "tf": "1",
                "candles": _make_uptrend_candles(),
                "prevCount": 0, "todayCount": 70,
            })
        raise AssertionError(req.url.path)

    bus = Bus()
    received = bus.collector(TOPIC_CANDIDATES)
    async with _kis(handler) as kc:
        agent = ScreeningAgent(
            kc, bus,
            ScreeningParams(threshold=40.0, top_n=2),  # 낮은 threshold로 통과 보장
        )
        candidates = await agent.screen_once()

    assert len(candidates) >= 1
    assert all(isinstance(c, ScreeningCandidate) for c in candidates)
    assert len(received) == len(candidates)
    assert all(c.score >= 40.0 for c in candidates)


async def test_screening_filters_below_threshold() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/kis/volume-rank":
            return httpx.Response(200, json=_make_vol_rank_response())
        if req.url.path == "/api/kis/chart":
            # 데이터 부족 (10개) → 점수 0에 가까움
            body = json.loads(req.content)
            return httpx.Response(200, json={
                "ok": True, "code": body["code"], "date": "20260529",
                "prevDate": "20260528", "tf": "1",
                "candles": _make_uptrend_candles()[:10],
                "prevCount": 0, "todayCount": 10,
            })
        raise AssertionError(req.url.path)

    bus = Bus()
    received = bus.collector(TOPIC_CANDIDATES)
    async with _kis(handler) as kc:
        agent = ScreeningAgent(
            kc, bus, ScreeningParams(threshold=70.0, top_n=2),
        )
        candidates = await agent.screen_once()
    assert candidates == []
    assert received == []


async def test_screening_fallback_never_empties_universe() -> None:
    """§19 불변식: 모두 임계 미달이어도(메모리 감점 등) 최고점 1개를 폴백으로 발행한다.

    단일 집중(§5.7) 파이프라인이 굶지 않게 보장 — 하류 게이트가 최종 판정한다.
    """
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/kis/volume-rank":
            return httpx.Response(200, json=_make_vol_rank_response())
        if req.url.path == "/api/kis/chart":
            body = json.loads(req.content)
            return httpx.Response(200, json={
                "ok": True, "code": body["code"], "date": "20260529",
                "prevDate": "20260528", "tf": "1",
                "candles": _make_uptrend_candles(),
                "prevCount": 0, "todayCount": 70,
            })
        raise AssertionError(req.url.path)

    bus = Bus()
    received = bus.collector(TOPIC_CANDIDATES)
    async with _kis(handler) as kc:
        # 임계 200(도달 불가) → 통과 0이지만 폴백으로 최고점 1개는 나와야 한다.
        agent = ScreeningAgent(kc, bus, ScreeningParams(threshold=200.0, top_n=2))
        candidates = await agent.screen_once()
    assert len(candidates) == 1
    assert len(received) == 1
    assert candidates[0].score < 200.0          # 임계 미달이지만 폴백으로 발행됨


async def test_screening_isolates_per_symbol_chart_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/kis/volume-rank":
            return httpx.Response(200, json=_make_vol_rank_response())
        if req.url.path == "/api/kis/chart":
            body = json.loads(req.content)
            if body["code"] == "000660":
                return httpx.Response(200, json={"ok": False, "error": "no chart"})
            return httpx.Response(200, json={
                "ok": True, "code": body["code"], "date": "20260529",
                "prevDate": "20260528", "tf": "1",
                "candles": _make_uptrend_candles(),
                "prevCount": 0, "todayCount": 70,
            })
        raise AssertionError(req.url.path)

    bus = Bus()
    async with _kis(handler) as kc:
        agent = ScreeningAgent(
            kc, bus, ScreeningParams(threshold=40.0, top_n=2),
        )
        candidates = await agent.screen_once()
    # 005930만 점수 매겨짐 (000660은 chart 실패로 격리)
    assert all(c.code == "005930" for c in candidates)
