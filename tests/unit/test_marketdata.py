"""core.marketdata + 로컬 캔들 백테스트 통합 (§18)."""
from __future__ import annotations

from datetime import date, time

import httpx
import pytest

from core.backtest import ReplayKisClient
from core.kis_client import PaperBroker, Side
from core.marketdata import CandleStore, YahooClient
from core.time_utils import SimClock, at_kst


# ─────────────────────────── CandleStore ───────────────────────────


def _rows(symbol: str, date_str: str, base: int) -> list[dict]:
    return [
        {"symbol": symbol, "date": date_str, "t": f"09:{i:02d}",
         "o": base + i, "h": base + i + 2, "l": base + i - 1, "c": base + i + 1,
         "v": 100 + i}
        for i in range(5)
    ]


def test_candle_store_write_read_skip(tmp_path) -> None:
    st = CandleStore(tmp_path)
    assert st.available_dates() == []
    assert st.write_day("20240104", _rows("005930", "20240104", 1000)) is True
    assert st.has_date("20240104")
    assert st.available_dates() == ["20240104"]
    # 스킵: 이미 있는 날짜는 덮어쓰지 않음
    assert st.write_day("20240104", _rows("005930", "20240104", 9999)) is False
    rows = st.read_symbol("20240104", "005930")
    assert len(rows) == 5 and rows[0]["o"] == 1000   # 덮어쓰기 안 됨
    assert st.symbols_on("20240104") == ["005930"]


async def test_yahoo_parses_kst_session(monkeypatch) -> None:
    # 2024-01-02 09:00 KST == 2024-01-02 00:00 UTC == epoch 1704153600
    base = 1704153600
    ts = [base, base + 60, base + 120, base + 7 * 3600]  # 마지막은 16:00 KST (장외)
    payload = {"chart": {"result": [{
        "meta": {"symbol": "005930.KS", "gmtoffset": 32400, "timezone": "KST"},
        "timestamp": ts,
        "indicators": {"quote": [{
            "open": [100, 101, 102, 200], "high": [101, 102, 103, 201],
            "low": [99, 100, 101, 199], "close": [100, 101, 102, 200],
            "volume": [10, 11, 12, 99],
        }]},
    }], "error": None}}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with YahooClient(http_client=http) as yc:
        rows = await yc.fetch("005930.KS", store_symbol="005930", range_="1d")
    # 장외(16:00) 캔들은 제외 → 3개, 09:00/09:01/09:02
    assert [r.t for r in rows] == ["09:00", "09:01", "09:02"]
    assert all(r.date == "20240102" and r.symbol == "005930" for r in rows)


# ─────────────────────────── ReplayKisClient + 로컬 저장소 ───────────────────────────


def _store_with_two_days(tmp_path) -> CandleStore:
    st = CandleStore(tmp_path)
    # 직전 데이터 보유일(20240102) + 당일(20240104). 20240103은 비어 있음(resolver가 건너뜀).
    st.write_day("20240102", _rows("005930", "20240102", 1000) + _rows("000660", "20240102", 500))
    today = []
    for i in range(6):     # 09:00~09:05
        today.append({"symbol": "005930", "date": "20240104", "t": f"09:{i:02d}",
                      "o": 1000, "h": 1010, "l": 990, "c": 1000 + i * 2, "v": 50})
    st.write_day("20240104", today)
    return st


async def test_replay_uses_local_store_with_cutoff(tmp_path) -> None:
    st = _store_with_two_days(tmp_path)
    clock = SimClock(at_kst(date(2024, 1, 4), time(9, 2)))
    rc = ReplayKisClient(
        data_client=object(), clock=clock, broker=PaperBroker(persist_path=None),
        candle_store=st, names={"005930": "삼성전자", "000660": "SK하이닉스"},
    )
    rc.set_session("20240104")
    chart = await rc.get_chart("005930")
    today = [c for c in chart.candles if not c.isPrev]
    prev = [c for c in chart.candles if c.isPrev]
    assert [c.t for c in today] == ["09:00", "09:01", "09:02"]   # 09:02 컷오프
    assert len(prev) == 5 and chart.prevDate == "20240102"        # 20240103 건너뛰고 20240102


async def test_replay_volume_rank_from_local_prev_day(tmp_path) -> None:
    st = _store_with_two_days(tmp_path)
    clock = SimClock(at_kst(date(2024, 1, 4), time(9, 0)))
    rc = ReplayKisClient(
        data_client=object(), clock=clock, broker=PaperBroker(persist_path=None),
        candle_store=st, names={"005930": "삼성전자", "000660": "SK하이닉스"},
    )
    rc.set_session("20240104")
    ranks = await rc.get_volume_rank(market="0000", top_n=10)
    codes = [it.code for it in ranks.items]
    # 전일 거래대금(Σc·v) 기준: 005930(~1000대) > 000660(~500대)
    assert codes == ["005930", "000660"]
    assert ranks.items[0].name == "삼성전자"


async def test_replay_local_fill_and_balance(tmp_path) -> None:
    st = _store_with_two_days(tmp_path)
    clock = SimClock(at_kst(date(2024, 1, 4), time(9, 3)))
    broker = PaperBroker(persist_path=None, start_cash=1_000_000)
    rc = ReplayKisClient(
        data_client=object(), clock=clock, broker=broker, candle_store=st,
    )
    rc.set_session("20240104")
    await rc.place_order(side=Side.BUY, code="005930", qty=10, price=0)  # 시장가 → 09:03 종가 1006
    bal = await rc.get_balance()
    pos = {p.code: p for p in bal.positions}
    assert pos["005930"].qty == 10
    assert pos["005930"].avgPrice == 1006
