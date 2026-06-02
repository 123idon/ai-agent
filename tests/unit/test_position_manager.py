"""Unit tests for PositionManagerAgent (CLAUDE.md §2.5, §5.3~5.5, §5.7)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from agents.analysis.signal.indicators import Direction, Signal, SignalAnalyzer, SignalParams
from agents.analysis.signal.main import EntrySignal
from agents.execution.order.main import OrderAgent, OrderEvent
from agents.execution.position_manager.exit_rules import ExitParams
from agents.execution.position_manager.main import TOPIC_EXIT, PositionManagerAgent
from agents.risk.risk_manager.hard_limits import StopLossTracker
from agents.risk.risk_manager.main import ApprovedOrder
from core.kis_client import (
    KisClient,
    KisClientConfig,
    Mode,
    OrderType,
    Side,
)
from core.messaging import Bus

ROOT = Path(__file__).parents[2]
KST = timezone(timedelta(hours=9))


def _now(h: int = 11, m: int = 0) -> datetime:
    return datetime(2026, 5, 29, h, m, 0, tzinfo=KST)


def _config() -> KisClientConfig:
    return KisClientConfig(
        base_url="http://traidair.test", app_key="AK", app_secret="AS",
        account="12345678-01", mode=Mode.PAPER,
    )


def _candles(closes: list[int], vol: int = 1000) -> list[dict]:
    out = []
    for c in closes:
        out.append({
            "t": "11:00", "date": "20260529",
            "o": c, "h": c, "l": c, "c": c, "v": vol,
        })
    return out


def _balance_json(qty: int, current: int) -> dict:
    positions = []
    if qty > 0:
        positions = [{
            "code": "005930", "name": "삼성전자", "qty": qty,
            "avgPrice": 10_000, "currentPrice": current,
            "evalAmt": qty * current, "pnl": (current - 10_000) * qty,
            "pnlPct": "0.00", "loanDt": "", "crdtType": "",
        }]
    return {
        "ok": True, "cash": 5_000_000,
        "totalEval": 5_000_000 + sum(p["evalAmt"] for p in positions),
        "totalPnl": 0, "positions": positions,
    }


def _chart_json(closes: list[int]) -> dict:
    candles = _candles(closes)
    return {
        "ok": True, "code": "005930", "date": "20260529", "tf": "1",
        "candles": candles, "prevCount": 0, "todayCount": len(candles),
    }


def _handler(balance_json: dict, chart_json: dict, sells: list[dict]):
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/api/kis/balance":
            return httpx.Response(200, json=balance_json)
        if path == "/api/kis/chart":
            return httpx.Response(200, json=chart_json)
        if path == "/api/kis/order":
            sells.append(json.loads(req.content))
            return httpx.Response(200, json={"ok": True, "ordNo": "1", "msg": "ok"})
        if path == "/api/kis/token":
            return httpx.Response(200, json={"ok": True, "token": "abcdefghij"})
        return httpx.Response(200, json={"ok": False, "error": f"unexpected {path}"})
    return handler


def _client(handler) -> KisClient:
    http = httpx.AsyncClient(
        base_url="http://traidair.test",
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(6.0),
    )
    return KisClient(_config(), http_client=http)


def _make_pos_mgr(kis: KisClient, bus: Bus, tracker: StopLossTracker) -> PositionManagerAgent:
    analyzer = SignalAnalyzer(SignalParams.from_file(ROOT / "config" / "strategy_params.yaml"))
    exit_params = ExitParams.from_file(ROOT / "config" / "strategy_params.yaml")
    order = OrderAgent(kis, bus)
    return PositionManagerAgent(
        kis, bus, order, analyzer, exit_params, tracker,
        clock=lambda: _now(),
    )


async def _register(
    bus: Bus, *, qty: int = 10, entry: int = 10_000, low: int = 9_900,
    atr_pct: float | None = None,
) -> None:
    sig = EntrySignal(
        symbol="005930", direction=Direction.LONG, signal=Signal.STRONG_ENTRY,
        score_count=4, entry_price=entry, entry_candle_low=low, entry_candle_high=entry + 100,
        use_credit_hint=False, timestamp=_now(), reason="entry", atr_pct=atr_pct,
    )
    approved = ApprovedOrder(
        symbol="005930", side=Side.BUY, code="005930", qty=qty, price=entry,
        order_type=OrderType.LIMIT, use_credit=False, is_new_entry=True,
        entry_signal=sig, timestamp=_now(), reason="entry",
    )
    event = OrderEvent(
        ord_no="1", symbol="005930", side=Side.BUY, qty=qty, price=entry,
        use_credit=False, mode=Mode.PAPER, timestamp=_now(), msg="ok", approved=approved,
    )
    await bus.publish("order.event", event)


# ─────────────────────────── 손절 ───────────────────────────


async def test_hard_stop_sends_market_sell_and_counts() -> None:
    bus = Bus()
    sells: list[dict] = []
    exits = bus.collector(TOPIC_EXIT)
    tracker = StopLossTracker()
    # 현재가 9600 = -4% → 하드 손절
    kis = _client(_handler(_balance_json(10, 9_600), _chart_json([10_000] * 29 + [9_600]), sells))
    async with kis:
        pos_mgr = _make_pos_mgr(kis, bus, tracker)
        await _register(bus)
        assert pos_mgr.is_flat() is False
        ev = await pos_mgr.monitor_once()

    assert ev is not None and ev.kind == "hard_stop_loss"
    assert len(sells) == 1
    assert sells[0]["side"] == "sell"
    assert sells[0]["qty"] == 10
    assert sells[0]["orderType"] == "market"
    assert tracker.consecutive_count == 1     # HL-02 카운터 산입
    assert pos_mgr.is_flat() is True           # 전량 청산 후 상태 클리어
    assert len(exits) == 1 and exits[0].kind == "hard_stop_loss"


# ─────────────────────────── 익절 ───────────────────────────


async def test_high_atr_raises_tp1_target() -> None:
    # 고변동성(atr_pct=0.04)으로 진입 → TP1 목표가 +5%로 사전 지정.
    # +3.5%(저변동성 기본 목표)에서는 청산되지 않아야 한다.
    bus = Bus()
    sells: list[dict] = []
    tracker = StopLossTracker()
    rising = [10_000 + i * 10 for i in range(29)] + [10_350]   # +3.5%
    kis = _client(_handler(_balance_json(10, 10_350), _chart_json(rising), sells))
    async with kis:
        pos_mgr = _make_pos_mgr(kis, bus, tracker)
        await _register(bus, atr_pct=0.04)
        ev = await pos_mgr.monitor_once()
    assert ev is None            # ATR 동적 목표(+5%) 덕분에 +3.5%에서 미청산
    assert sells == []


async def test_tp1_partial_sell_resets_counter() -> None:
    bus = Bus()
    sells: list[dict] = []
    exits = bus.collector(TOPIC_EXIT)
    tracker = StopLossTracker()
    tracker.record_stoploss(_now())            # 사전 손절 1회 → 익절로 리셋되는지 확인
    rising = [10_000 + i * 10 for i in range(29)] + [10_350]   # 마지막 +3.5%
    kis = _client(_handler(_balance_json(10, 10_350), _chart_json(rising), sells))
    async with kis:
        pos_mgr = _make_pos_mgr(kis, bus, tracker)
        await _register(bus)
        ev = await pos_mgr.monitor_once()

    assert ev is not None and ev.kind == "take_profit_1"
    assert len(sells) == 1
    assert sells[0]["side"] == "sell"
    assert sells[0]["qty"] == 5                 # 50% 부분 청산(§5.3: 1차 50%)
    assert tracker.consecutive_count == 0       # 익절로 리셋
    assert pos_mgr.is_flat() is False           # 잔여 보유 지속
    assert len(exits) == 1


# ─────────────────────────── 무보유 ───────────────────────────


async def test_no_position_is_noop() -> None:
    bus = Bus()
    sells: list[dict] = []
    tracker = StopLossTracker()
    kis = _client(_handler(_balance_json(0, 0), _chart_json([10_000] * 30), sells))
    async with kis:
        pos_mgr = _make_pos_mgr(kis, bus, tracker)
        ev = await pos_mgr.monitor_once()

    assert ev is None
    assert sells == []
    assert pos_mgr.is_flat() is True


async def test_position_cleared_when_gone_from_balance() -> None:
    bus = Bus()
    sells: list[dict] = []
    tracker = StopLossTracker()
    # 등록은 했지만 잔고엔 없음(외부 청산됨) → 상태 클리어
    kis = _client(_handler(_balance_json(0, 0), _chart_json([10_000] * 30), sells))
    async with kis:
        pos_mgr = _make_pos_mgr(kis, bus, tracker)
        await _register(bus)
        assert pos_mgr.is_flat() is False
        ev = await pos_mgr.monitor_once()

    assert ev is None
    assert sells == []
    assert pos_mgr.is_flat() is True
