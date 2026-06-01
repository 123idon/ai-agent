"""core.backtest.dashboard — Bus 수집 / 스냅샷 / 파일 기록 (§17)."""
from __future__ import annotations

import json
from datetime import date, time
from types import SimpleNamespace

from core.backtest import BacktestDashboard
from core.backtest.runner import DayResult
from core.kis_client import BalanceSnapshot, Position
from core.messaging import Bus
from core.time_utils import SimClock, at_kst


class _FakeReplay:
    """get_balance + get_chart 스텁 (보유 1종목, 분봉 2개)."""

    async def get_balance(self) -> BalanceSnapshot:
        return BalanceSnapshot(
            cash=900_000, totalEval=1_010_000, totalPnl=10_000,
            positions=[Position(
                code="005930", name="", qty=10, avgPrice=100, currentPrice=110,
                evalAmt=1100, pnl=100, pnlPct="10.00", loanDt="", crdtType="",
            )],
        )

    async def get_chart(self, code, *, date=None, tf="1"):
        prev = SimpleNamespace(t="15:30", o=98, h=99, l=97, c=98, v=500, isPrev=True)
        c1 = SimpleNamespace(t="09:00", o=100, h=105, l=99, c=104, v=1000, isPrev=False)
        c2 = SimpleNamespace(t="09:01", o=104, h=108, l=103, c=107, v=1200, isPrev=False)
        return SimpleNamespace(
            code=code, date="20240104", prevDate="20240103", tf="1",
            candles=[prev, c1, c2], prevCount=1, todayCount=2,
        )


def _dash(tmp_path) -> tuple[BacktestDashboard, Bus, SimClock]:
    bus = Bus()
    clock = SimClock(at_kst(date(2024, 1, 4), time(10, 30)))
    dash = BacktestDashboard(
        clock, broker=SimpleNamespace(), replay=_FakeReplay(), bus=bus,
        state_path=tmp_path / "state" / "backtest_live.json",
        start_cash=1_000_000,
    )
    dash.start_day("20240104")
    return dash, bus, clock


async def test_snapshot_collects_bus_events(tmp_path) -> None:
    dash, bus, _clock = _dash(tmp_path)

    await bus.publish("screening.candidates", SimpleNamespace(
        code="005930", name="삼성전자", score=82.0, themes=("반도체",)))
    await bus.publish("signal.analysis", SimpleNamespace(
        symbol="005930", signal="STRONG_ENTRY", score_count=4,
        indicators=[
            SimpleNamespace(name="volume", passed=True, detail="2.1x", value=2.1),
            SimpleNamespace(name="rsi", passed=True, detail="58", value=58.3),
            SimpleNamespace(name="macd", passed=True, detail="GC", value=0.1),
            SimpleNamespace(name="ma", passed=True, detail="정배열", value=1.0),
            SimpleNamespace(name="candle", passed=False, detail="none", value=None),
        ],
        reason="4/5", timestamp=None))
    await bus.publish("risk.decision.approved", SimpleNamespace(
        symbol="005930", qty=10, price=100, use_credit=False, reason="ok",
        timestamp=None))
    await bus.publish("order.event", SimpleNamespace(
        side="buy", symbol="005930", qty=10, price=100, use_credit=False,
        timestamp=None))
    await bus.publish("signal.exit", SimpleNamespace(
        symbol="005930", kind="take_profit_1", qty=5, price=110, pnl_pct=0.10,
        counter="takeprofit", reason="tp1", timestamp=None))
    await bus.publish("market.state", SimpleNamespace(
        grade="GREEN", reason="정상", kospi_chg_pct=0.5, kosdaq_chg_pct=0.3))

    snap = await dash.snapshot()

    assert snap["sim"]["date"] == "20240104"
    assert snap["sim"]["time"] == "10:30:00"
    assert snap["screening"][0]["code"] == "005930"
    assert snap["screening"][0]["name"] == "삼성전자"
    # 매매 내역: 진입 1 + 청산 1
    kinds = sorted(t["kind"] for t in snap["todayTrades"])
    assert kinds == ["entry", "exit"]
    # 보유 포지션 (이름은 스크리닝에서 매핑)
    assert snap["positions"][0]["name"] == "삼성전자"
    assert snap["positions"][0]["pnlPct"] == 10.0
    # 가상잔고(HTS 보유 탭 연동): 현금/총평가/오늘손익/한도
    bal = snap["balance"]
    assert bal["cash"] == 900_000
    assert bal["totalEval"] == 1_010_000
    assert bal["startCash"] == 1_000_000
    assert bal["todayPnl"] == 10_000          # totalEval - day_start_equity(1,000,000)
    assert bal["creditLimit"] == 900_000      # 백테스트 현금 기준
    # 실시간 차트 재생용 분봉 (오늘 봉만, prevClose 별도) — 포커스=보유종목
    chart = snap["chart"]
    assert chart["code"] == "005930"
    assert chart["prevClose"] == 98           # 전일 마지막 종가
    assert [c["t"] for c in chart["candles"]] == ["09:00", "09:01"]
    assert chart["candles"][-1]["c"] == 107
    # 에이전트 상태
    sig = snap["agents"]["signal"]
    assert sig["signal"] == "STRONG_ENTRY"
    assert sig["scoreCount"] == 4
    assert len(sig["indicators"]) == 5
    assert snap["agents"]["risk"]["decision"] == "APPROVE"
    assert snap["agents"]["market"]["grade"] == "GREEN"
    assert snap["agents"]["order"]["side"] == "buy"
    assert snap["agents"]["ceo"]["mode"] == "paper"


async def test_cumulative_after_day(tmp_path) -> None:
    dash, bus, _clock = _dash(tmp_path)
    # 청산 2건(승1·패1)
    await bus.publish("signal.exit", SimpleNamespace(
        symbol="A", kind="tp1", qty=1, price=1, pnl_pct=0.04, counter="takeprofit",
        reason="", timestamp=None))
    await bus.publish("signal.exit", SimpleNamespace(
        symbol="B", kind="hard", qty=1, price=1, pnl_pct=-0.03, counter="stoploss",
        reason="", timestamp=None))
    await dash.end_day(DayResult(
        date="20240104", start_cash=1_000_000, end_value=1_010_000,
        pnl=10_000, pnl_pct=1.0, n_entries=2, n_exits=2))

    c = (await dash.snapshot())["cumulative"]
    assert c["days"] == 1
    assert c["totalPnl"] == 10_000
    assert c["trades"] == 2
    assert c["tradeWinRate"] == 50.0
    # profit factor = 0.04 / 0.03
    assert c["profitFactor"] == round(0.04 / 0.03, 2)


async def test_progress_day_index_and_total(tmp_path) -> None:
    """진행 표시(요구 3): cumulative.dayIndex(현재 일차) / totalDays(설정 총일수).

    데이터 거래일만 카운트되며, 하루 마감(end_day) 후 다음 start_day에서 일차가 증가한다.
    """
    bus = Bus()
    clock = SimClock(at_kst(date(2024, 1, 4), time(10, 30)))
    dash = BacktestDashboard(
        clock, broker=SimpleNamespace(), replay=_FakeReplay(), bus=bus,
        state_path=tmp_path / "state" / "backtest_live.json",
        start_cash=1_000_000, total_days=10,
    )
    # 1일차
    dash.start_day("20240104")
    c = (await dash.snapshot())["cumulative"]
    assert c["dayIndex"] == 1
    assert c["totalDays"] == 10

    # 1일 마감 → 2일차로 진행
    await dash.end_day(DayResult(
        date="20240104", start_cash=1_000_000, end_value=1_001_000,
        pnl=1_000, pnl_pct=0.1, n_entries=1, n_exits=1))
    dash.start_day("20240105")
    c = (await dash.snapshot())["cumulative"]
    assert c["dayIndex"] == 2
    assert c["totalDays"] == 10
    assert c["days"] == 1   # 완료한 거래일 수(진행 중인 2일차는 미포함)


async def test_progress_total_days_none_for_unlimited(tmp_path) -> None:
    """무제한 모드(total_days 미지정)는 totalDays=None — HTS는 'N일차'만 표시한다(요구 3)."""
    dash, _bus, _clock = _dash(tmp_path)   # total_days 미전달
    c = (await dash.snapshot())["cumulative"]
    assert c["dayIndex"] == 1
    assert c["totalDays"] is None


async def test_write_produces_file(tmp_path) -> None:
    dash, _bus, _clock = _dash(tmp_path)
    dash._write(await dash.snapshot())
    p = tmp_path / "state" / "backtest_live.json"
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["ok"] is True and data["sim"]["date"] == "20240104"
