"""Unit tests for HardLimitGate (CLAUDE.md §4)."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path

from agents.risk.risk_manager.hard_limits import (
    BlackoutWindow,
    HardLimitGate,
    HardLimitsConfig,
    KST,
    MarketContext,
    OrderIntent,
    tick_size,
)
from core.kis_client import (
    BalanceSnapshot,
    OrderType,
    OrderableAmount,
    OrderbookLevel,
    OrderbookSnapshot,
    Position,
    Side,
)


def _cfg() -> HardLimitsConfig:
    return HardLimitsConfig(
        max_concurrent_positions=3,
        consecutive_stoploss_threshold=3,
        cooldown_after_stoploss_minutes=60,
        entry_blackout_windows=(
            BlackoutWindow(time(9, 0), time(9, 30), "장초반"),
            BlackoutWindow(time(14, 30), time(15, 30), "장후반"),
        ),
        max_slippage_ticks=5,
        margin_maintenance_buffer_pct=0.05,
        version="2.0.0",
    )


def _balance(
    positions: list[Position] | None = None, *, cash: int = 100_000_000,
) -> BalanceSnapshot:
    positions = positions or []
    return BalanceSnapshot(
        cash=cash,
        totalEval=cash + sum(p.evalAmt for p in positions),
        totalPnl=0,
        positions=positions,
    )


def _orderbook(*, ask: int = 70_000, bid: int = 69_950) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        asks=[OrderbookLevel(price=ask, qty=100)],
        bids=[OrderbookLevel(price=bid, qty=100)],
        totalAsk=100, totalBid=100, strength=100.0,
    )


def _ctx(
    *,
    now: datetime,
    balance: BalanceSnapshot | None = None,
    orderbook: OrderbookSnapshot | None = None,
    orderable: OrderableAmount | None = None,
) -> MarketContext:
    return MarketContext(
        now=now,
        balance=balance or _balance(),
        orderbook=orderbook,
        orderable_amount=orderable,
    )


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 5, 29, hour, minute, tzinfo=KST)


def _buy(
    code: str, qty: int = 10, price: int = 70_000, *, use_credit: bool = False,
) -> OrderIntent:
    return OrderIntent(
        side=Side.BUY, code=code, qty=qty, price=price, use_credit=use_credit,
    )


def _position(code: str, *, qty: int = 1, evalAmt: int = 1) -> Position:
    return Position(
        code=code, name=code, qty=qty, avgPrice=1, currentPrice=1,
        evalAmt=evalAmt, pnl=0,
    )


# ─────────────────────────── HL-03 / HL-04: blackout ───────────────────────────


def test_blackout_morning_rejects_HL04() -> None:
    gate = HardLimitGate(_cfg())
    decision = gate.evaluate(
        _buy("005930"),
        _ctx(now=_at(9, 15), orderbook=_orderbook()),
    )
    assert not decision.approved
    assert decision.violations[0].rule_id == "HL-04"


def test_blackout_afternoon_rejects_HL03() -> None:
    gate = HardLimitGate(_cfg())
    decision = gate.evaluate(
        _buy("005930"),
        _ctx(now=_at(14, 35), orderbook=_orderbook()),
    )
    assert not decision.approved
    assert decision.violations[0].rule_id == "HL-03"


def test_blackout_does_not_block_exits() -> None:
    gate = HardLimitGate(_cfg())
    sell = OrderIntent(
        side=Side.SELL, code="005930", qty=10, price=70_000, is_new_entry=False,
    )
    decision = gate.evaluate(sell, _ctx(now=_at(14, 45), orderbook=_orderbook()))
    assert decision.approved


def test_in_window_approves_new_entry() -> None:
    gate = HardLimitGate(_cfg())
    decision = gate.evaluate(
        _buy("005930"),
        _ctx(now=_at(10, 30), orderbook=_orderbook()),
    )
    assert decision.approved


# ─────────────────────────── HL-01: concurrent positions ───────────────────────────


def test_concurrent_positions_HL01_blocks_4th() -> None:
    held = [_position(c) for c in ("A", "B", "C")]
    gate = HardLimitGate(_cfg())
    decision = gate.evaluate(
        _buy("D"),
        _ctx(now=_at(10, 30), balance=_balance(held), orderbook=_orderbook()),
    )
    assert not decision.approved
    assert decision.violations[0].rule_id == "HL-01"


def test_concurrent_positions_HL01_allows_addon_to_existing() -> None:
    """기존 보유 종목 추가 매수는 카운트하지 않음."""
    held = [_position(c) for c in ("A", "B", "C")]
    gate = HardLimitGate(_cfg())
    # 슬리피지 검증을 피하기 위해 매수가 = ask로 일치
    decision = gate.evaluate(
        _buy("A", price=70_000),
        _ctx(now=_at(10, 30), balance=_balance(held), orderbook=_orderbook(ask=70_000)),
    )
    assert decision.approved


# ─────────────────────────── HL-02: stop-loss cooldown ───────────────────────────


def test_stoploss_cooldown_HL02_blocks_within_window() -> None:
    gate = HardLimitGate(_cfg())
    now = _at(11, 0)
    for i in range(3):
        gate.tracker.record_stoploss(now - timedelta(minutes=30 - i))
    decision = gate.evaluate(_buy("005930"), _ctx(now=now, orderbook=_orderbook()))
    assert not decision.approved
    assert decision.violations[0].rule_id == "HL-02"


def test_stoploss_cooldown_HL02_resolves_after_1h() -> None:
    gate = HardLimitGate(_cfg())
    now = _at(12, 0)
    # 가장 최근 손절이 70분 전 (1시간 초과)
    gate.tracker.record_stoploss(now - timedelta(minutes=90))
    gate.tracker.record_stoploss(now - timedelta(minutes=80))
    gate.tracker.record_stoploss(now - timedelta(minutes=70))
    decision = gate.evaluate(_buy("005930"), _ctx(now=now, orderbook=_orderbook()))
    assert decision.approved


def test_take_profit_resets_consecutive_counter() -> None:
    gate = HardLimitGate(_cfg())
    now = _at(11, 0)
    for i in range(2):
        gate.tracker.record_stoploss(now - timedelta(minutes=30 - i))
    gate.tracker.record_take_profit()
    gate.tracker.record_stoploss(now - timedelta(minutes=5))
    # 익절 후 카운터 0 → 손절 1개뿐이므로 쿨다운 미발동
    decision = gate.evaluate(_buy("005930"), _ctx(now=now, orderbook=_orderbook()))
    assert decision.approved


# ─────────────────────────── HL-05: slippage ───────────────────────────


def test_slippage_HL05_rejects_when_gap_exceeds_5_ticks() -> None:
    gate = HardLimitGate(_cfg())
    # ask 30_000, tick=50, max_dev=250. 지정가 29_500 → gap=500 > 250.
    decision = gate.evaluate(
        _buy("005930", price=29_500),
        _ctx(now=_at(10, 30), orderbook=_orderbook(ask=30_000, bid=29_400)),
    )
    assert not decision.approved
    assert decision.violations[0].rule_id == "HL-05"


def test_slippage_HL05_approves_within_window() -> None:
    gate = HardLimitGate(_cfg())
    # ask 30_000, tick=50, max_dev=250. 지정가 29_850 → gap=150 ≤ 250.
    decision = gate.evaluate(
        _buy("005930", price=29_850),
        _ctx(now=_at(10, 30), orderbook=_orderbook(ask=30_000, bid=29_800)),
    )
    assert decision.approved


def test_slippage_market_order_exempt() -> None:
    gate = HardLimitGate(_cfg())
    market_buy = OrderIntent(
        side=Side.BUY, code="005930", qty=10,
        price=0, order_type=OrderType.MARKET,
    )
    decision = gate.evaluate(
        market_buy,
        _ctx(now=_at(10, 30), orderbook=_orderbook(ask=70_000, bid=60_000)),
    )
    assert decision.approved  # 시장가는 사전 슬리피지 측정 불가 → 면제


# ─────────────────────────── HL-06: credit margin ───────────────────────────


def test_credit_HL06_blocks_when_no_orderable() -> None:
    gate = HardLimitGate(_cfg())
    decision = gate.evaluate(
        _buy("005930", use_credit=True),
        _ctx(now=_at(10, 30), orderbook=_orderbook()),
    )
    assert not decision.approved
    assert decision.violations[0].rule_id == "HL-06"


def test_credit_HL06_rejects_over_safe_limit() -> None:
    gate = HardLimitGate(_cfg())
    oa = OrderableAmount(
        orderCashable=10_000_000,
        orderSubst=0,
        reusableAmt=0,
        fundRcvableAmt=0,
        maxBuyAmt=30_000_000,
        maxBuyQty=300,
        cmaEvluAmt=0,
    )
    # 신용 가용 = 30M - 10M = 20M. 안전 한도 = 20M × 0.95 = 19M.
    # 주문 금액 = 300 × 70_000 = 21M → 거절
    intent = OrderIntent(
        side=Side.BUY, code="005930", qty=300, price=70_000, use_credit=True,
    )
    decision = gate.evaluate(
        intent,
        _ctx(now=_at(10, 30), orderbook=_orderbook(), orderable=oa),
    )
    assert not decision.approved
    assert decision.violations[0].rule_id == "HL-06"


def test_credit_HL06_approves_within_safe_limit() -> None:
    gate = HardLimitGate(_cfg())
    oa = OrderableAmount(
        orderCashable=10_000_000,
        orderSubst=0,
        reusableAmt=0,
        fundRcvableAmt=0,
        maxBuyAmt=30_000_000,
        maxBuyQty=300,
        cmaEvluAmt=0,
    )
    # 100 × 70_000 = 7M ≤ 19M → 통과
    intent = OrderIntent(
        side=Side.BUY, code="005930", qty=100, price=70_000, use_credit=True,
    )
    decision = gate.evaluate(
        intent,
        _ctx(now=_at(10, 30), orderbook=_orderbook(), orderable=oa),
    )
    assert decision.approved


def test_credit_HL06_sell_side_exempt() -> None:
    """신용 매도(포지션 청산)는 마진 룰 적용 X."""
    gate = HardLimitGate(_cfg())
    sell = OrderIntent(
        side=Side.SELL, code="005930", qty=10, price=70_000,
        use_credit=True, is_new_entry=False,
    )
    decision = gate.evaluate(sell, _ctx(now=_at(10, 30), orderbook=_orderbook()))
    assert decision.approved


# ─────────────────────────── 다중 위반 ───────────────────────────


def test_multiple_violations_reported_together() -> None:
    gate = HardLimitGate(_cfg())
    held = [_position(c) for c in ("A", "B", "C")]
    # blackout 시간 + 동시보유 한도 초과 + 슬리피지 동시 위반
    decision = gate.evaluate(
        _buy("D", price=29_500),
        _ctx(
            now=_at(9, 15),
            balance=_balance(held),
            orderbook=_orderbook(ask=30_000, bid=29_400),
        ),
    )
    assert not decision.approved
    rule_ids = {v.rule_id for v in decision.violations}
    assert {"HL-04", "HL-01", "HL-05"}.issubset(rule_ids)


# ─────────────────────────── 설정 로딩 / 틱 ───────────────────────────


def test_hard_limits_loads_from_repo_yaml() -> None:
    cfg = HardLimitsConfig.from_file(
        Path(__file__).parents[2] / "config" / "hard_limits.yaml"
    )
    assert cfg.max_concurrent_positions == 3
    assert cfg.consecutive_stoploss_threshold == 3
    assert cfg.cooldown_after_stoploss_minutes == 60
    assert cfg.max_slippage_ticks == 5
    assert cfg.margin_maintenance_buffer_pct == 0.05
    assert len(cfg.entry_blackout_windows) == 2


def test_tick_size_buckets() -> None:
    assert tick_size(1_500) == 1
    assert tick_size(3_500) == 5
    assert tick_size(10_000) == 10
    assert tick_size(30_000) == 50
    assert tick_size(100_000) == 100
    assert tick_size(300_000) == 500
    assert tick_size(800_000) == 1_000
