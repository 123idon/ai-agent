"""Tests for ThemeDetector and DART penalty + ScreeningAgent integration."""
from __future__ import annotations

import json
from typing import Callable

import httpx

from agents.intel.screening.main import (
    TOPIC_CANDIDATES,
    ScreeningAgent,
    ScreeningParams,
)
from agents.intel.screening.scorer import dart_penalty
from agents.intel.screening.theme import ThemeDetector
from core.kis_client import KisClient, KisClientConfig, Mode
from core.messaging import Bus


# ─────────────────────────── ThemeDetector ───────────────────────────


def test_theme_detector_matches_default_keywords() -> None:
    td = ThemeDetector()
    assert td.is_in_top_themes("005930", "삼성전자")
    assert td.is_in_top_themes("247540", "에코프로비엠")
    assert not td.is_in_top_themes("ZZZ", "BlahBlah Corp")


def test_theme_detector_returns_matched_themes() -> None:
    td = ThemeDetector()
    themes = td.detect_themes("005930", "삼성전자")
    assert "반도체" in themes


def test_theme_detector_with_top_themes_filter() -> None:
    td = ThemeDetector(top_themes=("바이오",))
    assert not td.is_in_top_themes("005930", "삼성전자")     # 반도체는 필터됨
    assert td.is_in_top_themes("068270", "셀트리온")


# ─────────────────────────── DART penalty ───────────────────────────


def test_dart_penalty_delist_dominates() -> None:
    p, reason = dart_penalty(["관리종목 지정", "부도 위험 안내"])
    assert p == -100.0
    assert "관리" in reason


def test_dart_penalty_negative_keyword() -> None:
    p, reason = dart_penalty(["분기보고서", "감자 결정"])
    assert p == -20.0
    assert "감자" in reason


def test_dart_penalty_clean() -> None:
    p, reason = dart_penalty(["분기보고서", "주요사항보고서"])
    assert p == 0.0
    assert reason == ""


# ─────────────────────────── ScreeningAgent ───────────────────────────


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


def _candles(date: str = "20260529") -> list[dict]:
    out: list[dict] = []
    for i in range(70):
        c = 10_000 + 50 * i
        out.append({
            "t": f"10:{i:02d}", "date": date,
            "o": c - 30, "h": c + 50, "l": c - 40, "c": c, "v": 100,
        })
    return out


def _vol_rank_response() -> dict:
    return {
        "ok": True, "market": "0000", "rankBy": "3", "count": 1,
        "items": [{
            "rank": 1, "code": "005930", "name": "삼성전자",
            "price": 70_000, "change": 500, "changePct": 0.72,
            "volume": 1_000_000, "turnover": 70_000_000_000,
            "volSurgePct": 50.0, "volTurnoverPct": 0.5,
        }],
    }


async def test_screening_includes_theme_in_payload() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/kis/volume-rank":
            return httpx.Response(200, json=_vol_rank_response())
        if req.url.path == "/api/kis/chart":
            body = json.loads(req.content)
            return httpx.Response(200, json={
                "ok": True, "code": body["code"], "date": "20260529",
                "prevDate": "20260528", "tf": "1",
                "candles": _candles(), "prevCount": 0, "todayCount": 70,
            })
        if req.url.path == "/api/dart/corpcode":
            return httpx.Response(200, json={"corp_code": "00126380"})
        if req.url.path == "/api/dart/list":
            return httpx.Response(200, json={"status": "ok", "list": [], "total": 0})
        raise AssertionError(req.url.path)

    bus = Bus()
    received = bus.collector(TOPIC_CANDIDATES)
    async with _kis(handler) as kc:
        agent = ScreeningAgent(
            kc, bus, ScreeningParams(threshold=40.0, top_n=1),
        )
        candidates = await agent.screen_once()
    assert len(candidates) == 1
    assert "반도체" in candidates[0].themes
    assert candidates[0].breakdown.get("sector_theme", 0) > 0
    assert len(received) == 1


async def test_screening_applies_dart_delist_penalty() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/kis/volume-rank":
            return httpx.Response(200, json=_vol_rank_response())
        if req.url.path == "/api/kis/chart":
            body = json.loads(req.content)
            return httpx.Response(200, json={
                "ok": True, "code": body["code"], "date": "20260529",
                "prevDate": "20260528", "tf": "1",
                "candles": _candles(), "prevCount": 0, "todayCount": 70,
            })
        if req.url.path == "/api/dart/corpcode":
            return httpx.Response(200, json={"corp_code": "00126380"})
        if req.url.path == "/api/dart/list":
            return httpx.Response(200, json={
                "status": "ok",
                "list": [
                    {"corp_code": "00126380", "corp_name": "삼성전자",
                     "report_nm": "관리종목 지정", "rcept_no": "1", "rcept_dt": "20260528"},
                ],
                "total": 1,
            })
        raise AssertionError(req.url.path)

    bus = Bus()
    received = bus.collector(TOPIC_CANDIDATES)
    async with _kis(handler) as kc:
        agent = ScreeningAgent(
            kc, bus, ScreeningParams(threshold=40.0, top_n=1),
        )
        candidates = await agent.screen_once()
    # 관리종목 -100 페널티로 final score < 40 → 발행 X
    assert candidates == []
    assert received == []


async def test_screening_dart_failure_is_non_fatal() -> None:
    """DART 조회 실패해도 점수 계산은 계속 진행."""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/kis/volume-rank":
            return httpx.Response(200, json=_vol_rank_response())
        if req.url.path == "/api/kis/chart":
            body = json.loads(req.content)
            return httpx.Response(200, json={
                "ok": True, "code": body["code"], "date": "20260529",
                "prevDate": "20260528", "tf": "1",
                "candles": _candles(), "prevCount": 0, "todayCount": 70,
            })
        if req.url.path == "/api/dart/corpcode":
            return httpx.Response(500, json={"corp_code": None})
        raise AssertionError(req.url.path)

    bus = Bus()
    async with _kis(handler) as kc:
        agent = ScreeningAgent(
            kc, bus, ScreeningParams(threshold=40.0, top_n=1),
        )
        candidates = await agent.screen_once()
    assert len(candidates) == 1  # DART 실패는 비치명 → 점수만으로 발행
