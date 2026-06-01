"""core.backtest.runner — 1일 리플레이 구동 / 날짜선택 / 손익 집계 (§17)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

import httpx

from core.backtest import BacktestRunner, ReplayKisClient
from core.backtest.runner import DayResult
from core.kis_client import KisClient, KisClientConfig, Mode, PaperBroker, Side
from core.messaging import Bus
from core.time_utils import SimClock, at_kst

SIM_DATE = "20240104"


@dataclass
class _FakeOrder:
    side: Side


def _chart() -> dict:
    candles = [{"t": f"15:{i:02d}", "date": "20240103", "o": 100, "h": 101,
                "l": 99, "c": 100, "v": 1000, "isPrev": True} for i in range(21)]
    for i, c in enumerate([100, 102, 104, 106, 108, 110]):
        candles.append({"t": f"09:{i:02d}", "date": SIM_DATE, "o": c - 1,
                        "h": c + 2, "l": c - 2, "c": c, "v": 5000, "isPrev": False})
    return {"ok": True, "code": "005930", "date": SIM_DATE, "prevDate": "20240103",
            "tf": "1", "candles": candles, "prevCount": 21, "todayCount": 6}


def _replay(clock: SimClock, broker: PaperBroker) -> ReplayKisClient:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chart())

    http = httpx.AsyncClient(base_url="http://t.test",
                             transport=httpx.MockTransport(handler))
    cfg = KisClientConfig(base_url="http://t.test", app_key="AK", app_secret="AS",
                          account="X-01", mode=Mode.PAPER)
    return ReplayKisClient(KisClient(cfg, http_client=http), clock, broker)


async def test_run_one_day_buy_then_eod_exit_pnl() -> None:
    bus = Bus()
    broker = PaperBroker(persist_path=None, start_cash=1_000_000)
    clock = SimClock(at_kst(date(2024, 1, 4), time(9, 0)))
    replay = _replay(clock, broker)

    state = {"bought": False}

    async def screen():
        return ["005930"]

    async def try_enter(_cands):
        if state["bought"]:
            return
        # 09:00 시점 시세(=100)로 10주 매수
        await replay.place_order(side=Side.BUY, code="005930", qty=10, price=0)
        state["bought"] = True
        await bus.publish("order.event", _FakeOrder(side=Side.BUY))

    async def monitor():
        # 15:00 이후 전량 청산 (이 시점 시세=110)
        if clock.now().time() >= time(15, 0):
            _cash, pos = broker.snapshot()
            qty = pos.get("005930").qty if "005930" in pos else 0
            if qty > 0:
                await replay.place_order(side=Side.SELL, code="005930", qty=qty, price=0)
                await bus.publish("signal.exit", object())

    runner = BacktestRunner(
        clock, replay, broker, bus,
        start_date=date(2024, 1, 4), end_date=date(2024, 1, 4),
        screen=screen, try_enter=try_enter, monitor=monitor,
        start_cash=1_000_000, step_minutes=30, screen_every_minutes=1,
    )
    result = await runner.run_one_day(SIM_DATE)

    assert result.date == SIM_DATE
    assert result.n_entries == 1
    assert result.n_exits == 1
    # 100원 매수 → 110원 청산, 10주 → +100원
    assert result.pnl == 100
    assert result.end_value == 1_000_100
    # 장 종료 후 무보유
    _cash, pos = broker.snapshot()
    assert not pos


async def test_run_one_day_stops_midway_on_event() -> None:
    """정지 이벤트가 세팅되면 분 루프가 즉시 멈춘다(요구 1)."""
    import asyncio

    bus = Bus()
    broker = PaperBroker(persist_path=None, start_cash=1_000_000)
    clock = SimClock(at_kst(date(2024, 1, 4), time(9, 0)))
    replay = _replay(clock, broker)
    stop = asyncio.Event()
    seen: list[str] = []

    async def screen():
        return ["005930"]

    async def try_enter(_cands):
        seen.append(clock.now().strftime("%H:%M"))
        if len(seen) >= 2:   # 두 스텝 진행 후 정지 요청
            stop.set()

    async def monitor():
        return None

    runner = BacktestRunner(
        clock, replay, broker, bus,
        start_date=date(2024, 1, 4), end_date=date(2024, 1, 4),
        screen=screen, try_enter=try_enter, monitor=monitor,
        start_cash=1_000_000, step_minutes=30, screen_every_minutes=1,
    )
    await runner.run_one_day(SIM_DATE, stop_event=stop)
    # 09:00~15:30을 30분 간격으로 다 돌면 14스텝인데, 2스텝 후 멈췄으므로 훨씬 적다.
    assert len(seen) <= 3


async def test_pick_trading_day_resamples_until_verified() -> None:
    bus = Bus()
    broker = PaperBroker(persist_path=None)
    clock = SimClock(at_kst(date(2024, 1, 4), time(9, 0)))
    replay = _replay(clock, broker)

    calls = {"n": 0}

    async def verify(_ds: str) -> bool:
        calls["n"] += 1
        return calls["n"] >= 3   # 처음 2회는 비거래일로 간주

    async def _noop():  # pragma: no cover
        return None

    runner = BacktestRunner(
        clock, replay, broker, bus,
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 31),
        screen=lambda: _list(), try_enter=lambda c: _noop(), monitor=_noop,
        verify_trading_day=verify, max_pick_attempts=10,
    )
    ds = await runner.pick_trading_day()
    assert ds is not None
    assert calls["n"] == 3


async def _list():
    return []


async def test_pick_trading_day_gives_up() -> None:
    bus = Bus()
    broker = PaperBroker(persist_path=None)
    clock = SimClock(at_kst(date(2024, 1, 4), time(9, 0)))
    replay = _replay(clock, broker)

    async def verify(_ds: str) -> bool:
        return False

    async def _noop():
        return None

    runner = BacktestRunner(
        clock, replay, broker, bus,
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 31),
        screen=_list, try_enter=lambda c: _noop(), monitor=_noop,
        verify_trading_day=verify, max_pick_attempts=4,
    )
    assert await runner.pick_trading_day() is None


def _day_result(ds: str):
    from core.backtest.runner import DayResult
    return DayResult(date=ds, start_cash=1_000_000, end_value=1_000_000,
                     pnl=0, pnl_pct=0.0, n_entries=0, n_exits=0)


def _runner(start, end, *, verify, **kw) -> BacktestRunner:
    bus = Bus()
    broker = PaperBroker(persist_path=None)
    clock = SimClock(at_kst(date(2024, 1, 4), time(9, 0)))
    replay = _replay(clock, broker)

    async def _noop():
        return None

    return BacktestRunner(
        clock, replay, broker, bus,
        start_date=start, end_date=end,
        screen=_list, try_enter=lambda c: _noop(), monitor=_noop,
        verify_trading_day=verify, **kw,
    )


async def test_run_forever_isolates_day_errors_and_continues() -> None:
    """run_one_day가 예외를 던져도 백테스트 전체가 죽지 않고 그 날짜만 스킵한다(요구 4)."""
    import asyncio

    async def verify(_ds: str) -> bool:
        return True   # 모든 영업일에 데이터 있음

    runner = _runner(date(2024, 1, 1), date(2024, 1, 10), verify=verify)

    calls = {"n": 0}

    async def fake_run_one_day(ds: str, stop_event=None):
        calls["n"] += 1
        if calls["n"] % 2 == 0:           # 짝수 호출마다 에러
            raise RuntimeError("데이터 깨짐(테스트)")
        return _day_result(ds)

    runner.run_one_day = fake_run_one_day   # type: ignore[assignment]

    stop = asyncio.Event()
    results = await runner.run_forever(stop, max_days=3)
    # 에러 난 날은 스킵되고 정상일 3개를 채울 때까지 계속 진행 → 절대 비정상 종료 안 함.
    assert len(results) == 3
    assert calls["n"] > 3   # 에러로 스킵된 날이 섞여 호출이 3보다 많음


async def test_run_forever_infinite_no_self_terminate_on_sparse_data() -> None:
    """무제한 모드는 데이터가 적어도 혼자 종료하지 않고 정지 시까지 계속한다(요구 1·2).

    예전엔 희소 데이터에서 random_business_day 20회 재추첨이 모두 실패하면 None→break로
    조기 종료(4일 만에 혼자 멈춤)했다. 이제 데이터 있는 날짜 풀에서만 추첨하므로 멈추지 않는다.
    """
    import asyncio

    data_days = {"20240104", "20240115"}   # 한 달 중 단 2일만 데이터 있음(희소)

    async def verify(ds: str) -> bool:
        return ds in data_days

    runner = _runner(date(2024, 1, 1), date(2024, 1, 31), verify=verify, min_pool_days=1)

    stop = asyncio.Event()
    n = {"c": 0}

    async def fake_run_one_day(ds: str, stop_event=None):
        assert ds in data_days            # 데이터 있는 날짜만 추첨됨
        n["c"] += 1
        if n["c"] >= 10:                  # 10일 재생 후 정지(테스트 무한 루프 방지)
            stop.set()
        return _day_result(ds)

    runner.run_one_day = fake_run_one_day   # type: ignore[assignment]

    results = await runner.run_forever(stop, max_days=None)
    # 데이터가 2일뿐이어도 10회까지 계속 재생됨(혼자 종료 안 함). 정지로만 끝남.
    assert n["c"] == 10
    assert len(results) == 9   # 10번째는 정지로 break돼 미집계


async def test_run_forever_runs_exact_days_and_carries_balance() -> None:
    """설정한 거래일 수만큼 정확히 진행하고, 잔고/누적손익이 다음 날로 이어진다(요구 1·4).

    데이터가 충분하면 max_days만큼 distinct 날짜를 돌고, carry_over=True면 전날 종료
    순자산이 다음 날 시작값(start_value)이 된다(상태 유지). 마지막 날까지 가야 끝난다.
    """
    import asyncio

    async def verify(_ds: str) -> bool:
        return True   # 모든 영업일에 데이터 있음

    runner = _runner(
        date(2024, 1, 1), date(2024, 1, 31), verify=verify, carry_over=True,
    )

    seen: list[str] = []
    pnl_each = 1_000      # 매일 +1,000원 → 누적이 다음 날 start_value로 이어지는지 확인

    async def fake_run_one_day(ds: str, stop_event=None):
        # carry_over 누적을 흉내: 직전 종료값(_carry_baseline)을 시작값으로, +pnl 후 종료.
        start_v = runner._carry_baseline if runner._carry_baseline is not None else 1_000_000
        end_v = start_v + pnl_each
        runner._carry_baseline = end_v
        seen.append(ds)
        return DayResult(date=ds, start_cash=start_v, end_value=end_v,
                         pnl=pnl_each, pnl_pct=0.1, n_entries=1, n_exits=1)

    runner.run_one_day = fake_run_one_day   # type: ignore[assignment]

    results = await runner.run_forever(asyncio.Event(), max_days=10)

    # 1) 설정한 10거래일을 정확히 진행(1일만 하고 끝나지 않는다 — 본 버그의 회귀 가드).
    assert len(results) == 10
    assert len(seen) == 10
    # 2) distinct 날짜(데이터 충분 시 같은 날 반복 없이 서로 다른 영업일).
    assert len(set(seen)) == 10
    # 3) 잔고/누적손익 이어받기: 둘째 날 start_value == 첫째 날 end_value.
    assert results[1].start_cash == results[0].end_value
    assert results[-1].end_value == 1_000_000 + pnl_each * 10


async def test_run_forever_fills_requested_days_by_repeating_when_few_data() -> None:
    """데이터 거래일이 요청보다 적으면 부족분을 반복 재생해 **설정 일수를 끝까지 채운다**(요구 1·3)."""
    import asyncio

    data_days = {"20240104", "20240115"}   # 데이터 2일뿐

    async def verify(ds: str) -> bool:
        return ds in data_days

    runner = _runner(
        date(2024, 1, 1), date(2024, 1, 31), verify=verify, min_pool_days=1,
    )

    seen: list[str] = []

    async def fake_run_one_day(ds: str, stop_event=None):
        assert ds in data_days
        seen.append(ds)
        return _day_result(ds)

    runner.run_one_day = fake_run_one_day   # type: ignore[assignment]

    results = await runner.run_forever(asyncio.Event(), max_days=6)
    # 데이터 2일뿐이어도 반복 재생으로 설정한 6거래일을 정확히 채운다(1일 조기 종료 금지).
    assert len(results) == 6
    assert len(seen) == 6
    assert set(seen) <= data_days


async def test_ensure_pool_backfills_when_insufficient() -> None:
    """데이터 부족 시 backfill(Yahoo 자동 수집) 콜백이 호출되고 풀이 보강된다(요구 3)."""
    available = {"20240104"}   # 처음엔 1일만

    async def verify(ds: str) -> bool:
        return ds in available

    called = {"n": 0}

    async def backfill() -> int:
        called["n"] += 1
        available.update({"20240115", "20240116"})   # 수집으로 2일 추가
        return 2

    runner = _runner(
        date(2024, 1, 1), date(2024, 1, 31),
        verify=verify, backfill=backfill, min_pool_days=1,
    )
    pool = await runner._ensure_pool(3)   # 3개 원하는데 1개뿐 → backfill 트리거
    assert called["n"] == 1
    assert len(pool) == 3


async def test_ensure_pool_backfill_failure_is_non_fatal() -> None:
    """backfill이 예외를 던져도 백테스트는 죽지 않고 보유 데이터로 계속한다(요구 3·4)."""
    available = {"20240104"}

    async def verify(ds: str) -> bool:
        return ds in available

    async def backfill() -> int:
        raise RuntimeError("Yahoo 네트워크 실패(테스트)")

    runner = _runner(
        date(2024, 1, 1), date(2024, 1, 31),
        verify=verify, backfill=backfill, min_pool_days=5,
    )
    pool = await runner._ensure_pool(5)   # 5개 원하지만 1개뿐, backfill 실패 → 그래도 진행
    assert pool == ["20240104"]           # 보유 1일로 계속(비정상 종료 없음)
