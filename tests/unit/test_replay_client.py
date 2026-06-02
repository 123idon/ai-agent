"""core.backtest.replay_client — 룩어헤드 차단 / 합성 시세 / KRX 유니버스 (§17)."""
from __future__ import annotations

import httpx
import pytest

from core.backtest import ReplayKisClient
from core.kis_client import KisClient, KisClientConfig, Mode, PaperBroker, Side
from core.time_utils import SimClock, at_kst
from datetime import date, time

SIM_DATE = "20240104"   # 목요일 (직전 영업일 20240103)


def _full_day_chart() -> dict:
    """전일 21봉(워밍업용 isPrev) + 당일 09:00~09:05 6봉."""
    candles = []
    for i in range(21):
        candles.append({"t": f"15:{i:02d}", "date": "20240103",
                        "o": 100, "h": 101, "l": 99, "c": 100, "v": 1000,
                        "isPrev": True})
    closes = [100, 102, 104, 106, 108, 110]
    for i, c in enumerate(closes):
        candles.append({"t": f"09:{i:02d}", "date": SIM_DATE,
                        "o": c - 1, "h": c + 2, "l": c - 2, "c": c, "v": 5000 + i,
                        "isPrev": False})
    return {"ok": True, "code": "005930", "date": SIM_DATE, "prevDate": "20240103",
            "tf": "1", "candles": candles, "prevCount": 21, "todayCount": 6}


def _data_client() -> KisClient:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/kis/chart":
            return httpx.Response(200, json=_full_day_chart())
        if req.url.path == "/api/kis/volume-rank":
            return httpx.Response(200, json={
                "ok": True, "market": "0000", "rankBy": "3", "items": [
                    {"rank": 1, "code": "005930", "name": "삼성전자", "price": 100,
                     "change": 1, "changePct": 1.0, "volume": 10, "turnover": 900,
                     "volSurgePct": 0.0, "volTurnoverPct": 0.0},
                    {"rank": 2, "code": "000660", "name": "SK하이닉스", "price": 200,
                     "change": 2, "changePct": 1.0, "volume": 5, "turnover": 500,
                     "volSurgePct": 0.0, "volTurnoverPct": 0.0},
                ]})
        return httpx.Response(200, json={"ok": False, "error": "unexpected"})

    http = httpx.AsyncClient(base_url="http://traidair.test",
                             transport=httpx.MockTransport(handler))
    cfg = KisClientConfig(base_url="http://traidair.test", app_key="AK",
                          app_secret="AS", account="X-01", mode=Mode.PAPER)
    return KisClient(cfg, http_client=http)


def _replay(clock: SimClock, broker: PaperBroker) -> ReplayKisClient:
    rc = ReplayKisClient(_data_client(), clock, broker)
    rc.set_session(SIM_DATE)
    return rc


async def test_chart_truncates_at_sim_time() -> None:
    clock = SimClock(at_kst(date(2024, 1, 4), time(9, 3)))
    rc = _replay(clock, PaperBroker(persist_path=None))
    chart = await rc.get_chart("005930")
    today = [c for c in chart.candles if not c.isPrev]
    # 09:00~09:03 만 노출 (09:04, 09:05 미래는 차단)
    assert [c.t for c in today] == ["09:00", "09:01", "09:02", "09:03"]
    assert chart.todayCount == 4
    assert chart.prevCount == 21
    assert all(c.date == "20240103" for c in chart.candles if c.isPrev)


async def test_no_lookahead_when_clock_advances() -> None:
    clock = SimClock(at_kst(date(2024, 1, 4), time(9, 1)))
    rc = _replay(clock, PaperBroker(persist_path=None))
    early = await rc.get_chart("005930")
    assert early.todayCount == 2          # 09:00, 09:01
    clock.set(at_kst(date(2024, 1, 4), time(9, 5)))
    later = await rc.get_chart("005930")
    assert later.todayCount == 6          # 더 전진해야 비로소 노출
    # 미래 분봉이 과거 시점에 절대 보이지 않았음을 보장
    assert early.todayCount < later.todayCount


async def test_get_price_synthesized_from_cutoff() -> None:
    clock = SimClock(at_kst(date(2024, 1, 4), time(9, 2)))
    rc = _replay(clock, PaperBroker(persist_path=None))
    snap = await rc.get_price("005930")
    assert snap.price == 104               # 09:02 종가
    assert snap.open == 99                 # 09:00 시가 (100-1)
    assert snap.change == 4                # 104 - prev_close(100)


async def test_get_balance_values_at_sim_price() -> None:
    clock = SimClock(at_kst(date(2024, 1, 4), time(9, 2)))
    broker = PaperBroker(persist_path=None, start_cash=1_000_000)
    rc = _replay(clock, broker)
    await rc.place_order(side=Side.BUY, code="005930", qty=10, price=0)  # 시장가→104
    bal = await rc.get_balance()
    pos = {p.code: p for p in bal.positions}
    assert pos["005930"].qty == 10
    assert pos["005930"].avgPrice == 104
    assert bal.cash == 1_000_000 - 10 * 104


async def test_volume_rank_from_kis() -> None:
    clock = SimClock(at_kst(date(2024, 1, 4), time(9, 0)))
    rc = _replay(clock, PaperBroker(persist_path=None))
    ranks = await rc.get_volume_rank(market="0000", top_n=10)
    # KIS 거래대금 상위(traidair) 위임 — 삼성전자(900) > SK하이닉스(500)
    assert [it.code for it in ranks.items] == ["005930", "000660"]
    assert ranks.items[0].turnover == 900


async def test_dart_list_empty_in_backtest() -> None:
    clock = SimClock(at_kst(date(2024, 1, 4), time(9, 0)))
    rc = _replay(clock, PaperBroker(persist_path=None))
    dart = await rc.get_dart_list(days=2, corp_code="00126380")
    assert dart.list == [] and dart.total == 0
