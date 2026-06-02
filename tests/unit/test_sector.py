"""Unit tests — 섹터 데이터 추출 + 섹터 강도 가산점 (CLAUDE.md §2.2.1 B안).

검증: 분류기(코드/이름/미분류) · 전일 종가 기준 섹터 등락률/대장주 산출 ·
가산점 구간(+5/+3/0/-2)+대장주(+2) · 에러 폴백(데이터/매핑 없음 → 0, 무예외) ·
스크리닝 통합(가산점이 점수에 반영되고 선정이 달라짐).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from agents.analysis.signal.indicators import KST
from agents.intel.screening.main import (
    TOPIC_CANDIDATES,
    ScreeningAgent,
    ScreeningParams,
)
from agents.learning.sector import (
    SectorClassifier,
    SectorDataProvider,
    SectorSnapshot,
)
from agents.learning.sector.main import SectorInfo
from core.kis_client import KisClient, KisClientConfig, Mode
from core.messaging import Bus


# ─────────────────────────── 분류기 ───────────────────────────

def test_classifier_by_code_and_name_and_unknown() -> None:
    c = SectorClassifier()
    assert c.sector_of("005930", "삼성전자") == "반도체"
    assert c.sector_of("373220", "LG에너지솔루션") == "2차전지"
    # 코드 미등록 → 종목명 키워드 폴백.
    assert c.sector_of("900000", "무명바이오") == "바이오"
    # 코드·이름 모두 매핑 불가 → None.
    assert c.sector_of("900001", "듣보종목") is None
    assert c.sector_of("", "") is None


def test_classifier_from_file_overrides_default(tmp_path: Path) -> None:
    p = tmp_path / "sectors.json"
    p.write_text(json.dumps({"sectors": {"123456": "테스트섹터"}}), encoding="utf-8")
    c = SectorClassifier.from_file(p)
    assert c.sector_of("123456", "") == "테스트섹터"
    # 임베디드 기본값은 유지된다.
    assert c.sector_of("005930", "") == "반도체"


def test_classifier_from_missing_file_uses_default(tmp_path: Path) -> None:
    c = SectorClassifier.from_file(tmp_path / "nope.json")
    assert c.sector_of("005930", "") == "반도체"


# ─────────────────────────── 가산점 구간 ───────────────────────────

def test_snapshot_bonus_tiers_and_leader() -> None:
    snap = SectorSnapshot(
        date="20250102",
        _sector_by_code={
            "A": "강", "E": "강", "B": "약상", "C": "보합", "D": "약",
        },
        _change_by_sector={"강": 2.0, "약상": 1.0, "보합": 0.0, "약": -1.0},
        _leader_codes=frozenset({"A"}),
    )
    # +2% 이상 + 대장주 → +5 +2 = +7
    assert snap.bonus_for("A")[0] == 7.0
    # +2% 이상, 대장주 아님 → +5
    assert snap.bonus_for("E")[0] == 5.0
    # +1~2% → +3
    assert snap.bonus_for("B")[0] == 3.0
    # -1~+1% → 0
    assert snap.bonus_for("C")[0] == 0.0
    # -1% 이하 → -2 (경계 -1.0 포함)
    assert snap.bonus_for("D")[0] == -2.0
    # 미매핑 종목 → (0, "")
    assert snap.bonus_for("Z") == (0.0, "")
    # 사유에 섹터·등락률 표기.
    assert "강" in snap.bonus_for("A")[1] and "대장주" in snap.bonus_for("A")[1]


def test_snapshot_to_dict_shape() -> None:
    snap = SectorSnapshot(
        date="20250102",
        sectors=(
            SectorInfo("반도체", 2.345, ("삼성전자", "SK하이닉스"), ("005930", "000660"), 2),
        ),
    )
    d = snap.to_dict()
    assert d["date"] == "20250102"
    assert d["sectors"][0] == {
        "name": "반도체", "change_pct": 2.35,
        "top5_stocks": ["삼성전자", "SK하이닉스"],
    }


# ─────────────────────────── 추출기(provider) ───────────────────────────

class _FakeStore:
    """daily_aggregate 기반 가짜 CandleStore (close/volume 만 제어)."""

    def __init__(self, data: dict[str, dict[str, tuple[int, int]]]) -> None:
        # data: {date: {code: (close, volume)}}
        self._data = data

    def available_dates(self) -> list[str]:
        return sorted(self._data)

    def symbols_on(self, date: str) -> list[str]:
        return list(self._data.get(date, {}).keys())

    def daily_aggregate(self, date: str, code: str) -> dict | None:
        cell = self._data.get(date, {}).get(code)
        if cell is None:
            return None
        close, vol = cell
        return {"t": date, "date": date, "o": close, "h": close,
                "l": close, "c": close, "v": vol}


def _names() -> dict[str, str]:
    return {"005930": "삼성전자", "000660": "SK하이닉스", "005380": "현대차"}


def test_provider_computes_change_and_top5() -> None:
    store = _FakeStore({
        "20250101": {"005930": (1000, 1), "000660": (1000, 1), "005380": (1000, 1)},
        # 전일: 반도체 +2%(005930 거래대금 ↑ → 대장주), 자동차 -2%.
        "20250102": {"005930": (1020, 100), "000660": (1020, 10), "005380": (980, 50)},
        "20250103": {"005930": (1100, 1)},   # D — 사용 안 함(전일=0102)
    })
    prov = SectorDataProvider(store, names=_names(), classifier=SectorClassifier())
    snap = prov.sector_data("20250103")
    assert snap.date == "20250102"
    by_name = {s.name: s for s in snap.sectors}
    assert round(by_name["반도체"].change_pct, 2) == 2.0
    assert round(by_name["자동차"].change_pct, 2) == -2.0
    # 거래대금 상위가 top5 선두(005930: 1020*100 > 000660: 1020*10).
    assert by_name["반도체"].top5_codes[0] == "005930"
    assert "삼성전자" in by_name["반도체"].top5_stocks
    # 가산점: 반도체 +2% → +5, 005930·000660 모두 top5(2명) → +2.
    assert snap.bonus_for("005930") == (7.0, snap.bonus_for("005930")[1])
    assert snap.bonus_for("005380")[0] == -2.0 + 2.0   # 자동차 -2% +2(단독 대장주) = 0


def test_provider_no_store_or_no_prev_returns_empty() -> None:
    assert SectorDataProvider(None).sector_data("20250103").to_dict() == {
        "date": "", "sectors": []}
    store = _FakeStore({"20250103": {"005930": (1000, 1)}})  # 이전 거래일 없음
    snap = SectorDataProvider(store).sector_data("20250103")
    assert snap.date == ""
    assert snap.bonus_for("005930") == (0.0, "")


def test_provider_never_raises_on_bad_store() -> None:
    class _Broken:
        def available_dates(self):  # noqa: ANN
            raise RuntimeError("disk error")

    snap = SectorDataProvider(_Broken()).sector_data("20250103")
    assert snap.date == ""   # 예외 삼키고 빈 스냅샷


def test_provider_caches_per_date() -> None:
    store = _FakeStore({
        "20250101": {"005930": (1000, 1)},
        "20250102": {"005930": (1020, 1)},
        "20250103": {"005930": (1000, 1)},
    })
    prov = SectorDataProvider(store, names=_names())
    a = prov.sector_data("20250103")
    b = prov.sector_data("20250103")
    assert a is b


# ─────────────────────────── 스크리닝 통합 ───────────────────────────

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


def _uptrend(date: str = "20260529", base: int = 10_000) -> list[dict]:
    return [
        {"t": f"10:{i:02d}", "date": date, "o": base + 50 * i - 30,
         "h": base + 50 * i + 50, "l": base + 50 * i - 40, "c": base + 50 * i, "v": 100}
        for i in range(70)
    ]


def _handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/api/kis/volume-rank":
        return httpx.Response(200, json={
            "ok": True, "market": "0000", "rankBy": "3", "count": 1,
            "items": [{
                "rank": 1, "code": "005930", "name": "삼성전자", "price": 70_000,
                "change": 500, "changePct": 0.72, "volume": 1_000_000,
                "turnover": 70_000_000_000, "volSurgePct": 50.0, "volTurnoverPct": 0.5,
            }],
        })
    if req.url.path == "/api/kis/chart":
        body = json.loads(req.content)
        return httpx.Response(200, json={
            "ok": True, "code": body["code"], "date": "20260529",
            "prevDate": "20260528", "tf": "1",
            "candles": _uptrend(), "prevCount": 0, "todayCount": 70,
        })
    raise AssertionError(req.url.path)


def _clock() -> datetime:
    return datetime(2026, 6, 1, 8, 30, tzinfo=KST)


async def test_screening_applies_sector_bonus_to_score() -> None:
    """섹터 가산점이 최종 점수·breakdown 에 반영된다(+7: 강세섹터 +5 +대장주 +2)."""
    strong = SectorSnapshot(
        date="20260529",
        _sector_by_code={"005930": "반도체"},
        _change_by_sector={"반도체": 2.5},
        _leader_codes=frozenset({"005930"}),
    )

    bus_base = Bus()
    bus_sec = Bus()
    async with _kis(_handler) as kc:
        base_agent = ScreeningAgent(
            kc, bus_base, ScreeningParams(threshold=0.0, top_n=1), clock=_clock,
        )
        base = (await base_agent.screen_once())[0]
    async with _kis(_handler) as kc:
        sec_agent = ScreeningAgent(
            kc, bus_sec, ScreeningParams(threshold=0.0, top_n=1), clock=_clock,
            sector_provider=lambda _d: strong,
        )
        boosted = (await sec_agent.screen_once())[0]

    assert boosted.breakdown.get("sector_bonus") == 7.0
    assert round(boosted.score - base.score, 1) == 7.0
    assert "반도체" in boosted.reason


async def test_screening_sector_bonus_flips_selection() -> None:
    """가산점이 임계 미달 종목을 통과시켜 선정을 바꾼다."""
    # 임계를 base 점수보다 살짝 위로 잡아, 가산점이 있어야만 통과하게 한다.
    async with _kis(_handler) as kc:
        base = (await ScreeningAgent(
            kc, Bus(), ScreeningParams(threshold=0.0, top_n=1), clock=_clock,
        ).screen_once())[0]
    thr = base.score + 3.0   # 가산점(+7) 없으면 미달, 있으면 통과.

    strong = SectorSnapshot(
        date="20260529",
        _sector_by_code={"005930": "반도체"},
        _change_by_sector={"반도체": 2.5},
        _leader_codes=frozenset({"005930"}),
    )
    # 가산점 없음 → 임계 미달 → 폴백 1개(임계 미달 표시)만.
    async with _kis(_handler) as kc:
        no_bonus = await ScreeningAgent(
            kc, Bus(), ScreeningParams(threshold=thr, top_n=1), clock=_clock,
        ).screen_once()
    # 가산점 있음 → 임계 통과(정상 선정).
    async with _kis(_handler) as kc:
        with_bonus = await ScreeningAgent(
            kc, Bus(), ScreeningParams(threshold=thr, top_n=1), clock=_clock,
            sector_provider=lambda _d: strong,
        ).screen_once()

    assert no_bonus[0].score < thr          # 폴백(임계 미달)
    assert with_bonus[0].score >= thr       # 가산점으로 통과


async def test_screening_survives_sector_provider_error() -> None:
    """섹터 데이터 호출이 터져도 백테스트 중단 없이 기존 점수로 진행(요구 4)."""
    def boom(_d: str) -> SectorSnapshot:
        raise RuntimeError("sector backend down")

    bus = Bus()
    received = bus.collector(TOPIC_CANDIDATES)
    async with _kis(_handler) as kc:
        agent = ScreeningAgent(
            kc, bus, ScreeningParams(threshold=0.0, top_n=1), clock=_clock,
            sector_provider=boom,
        )
        cands = await agent.screen_once()
    assert len(cands) == 1
    assert len(received) == 1
    assert "sector_bonus" not in cands[0].breakdown
