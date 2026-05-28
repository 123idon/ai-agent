"""Hard-limit gate (CLAUDE.md §4).

리스크부 에이전트의 핵심: 어떤 신호도 §4를 우회할 수 없다.
실행부는 본 gate의 ``Decision.approved == True`` 없이는 주문을 송신할 수 없다.

본 모듈은 의도적으로 다음 한도를 *두지 않는다* (§4.1):
  - 1종목 비중 상한 (사이징은 신호 강도/시장상황/신용 가용액에 따른 동적 결정)
  - 일일 손실 halt (모든 손절은 기술적 트리거 또는 하드 -3%로만 발동)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import yaml

from core.kis_client import (
    BalanceSnapshot,
    OrderType,
    OrderableAmount,
    OrderbookSnapshot,
    Side,
)

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


# ─────────────────────────── 설정 ───────────────────────────


@dataclass(frozen=True)
class BlackoutWindow:
    start: time
    end: time
    reason: str = ""

    def contains(self, t: time) -> bool:
        return self.start <= t < self.end


@dataclass(frozen=True)
class HardLimitsConfig:
    max_concurrent_positions: int                   # HL-01
    consecutive_stoploss_threshold: int             # HL-02
    cooldown_after_stoploss_minutes: int            # HL-02
    entry_blackout_windows: tuple[BlackoutWindow, ...]  # HL-03 / HL-04
    max_slippage_ticks: int                         # HL-05
    margin_maintenance_buffer_pct: float            # HL-06
    version: str = ""

    @classmethod
    def from_file(cls, path: Path) -> "HardLimitsConfig":
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        windows = tuple(
            BlackoutWindow(
                start=_parse_time(w["start"]),
                end=_parse_time(w["end"]),
                reason=w.get("reason", ""),
            )
            for w in doc["entry_blackout_windows"]
        )
        return cls(
            max_concurrent_positions=doc["max_concurrent_positions"],
            consecutive_stoploss_threshold=doc["consecutive_stoploss_threshold"],
            cooldown_after_stoploss_minutes=doc["cooldown_after_stoploss_minutes"],
            entry_blackout_windows=windows,
            max_slippage_ticks=doc["max_slippage_ticks"],
            margin_maintenance_buffer_pct=doc["margin_maintenance_buffer_pct"],
            version=str(doc.get("version", "")),
        )


def _parse_time(value: str | time) -> time:
    if isinstance(value, time):
        return value
    parts = str(value).split(":")
    return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)


# ─────────────────────────── 입력 ───────────────────────────


@dataclass(frozen=True)
class OrderIntent:
    """신호분석 → 리스크부로 들어오는 주문 의도."""

    side: Side
    code: str
    qty: int
    price: int
    order_type: OrderType = OrderType.LIMIT
    use_credit: bool = False
    is_new_entry: bool = True   # False = 청산 (익절/손절/타임스톱)


@dataclass(frozen=True)
class MarketContext:
    """주문 검증 시점의 시장/계좌 상태."""

    now: datetime                                    # tz-aware (KST 권장)
    balance: BalanceSnapshot
    orderable_amount: OrderableAmount | None = None  # HL-06 검증용
    orderbook: OrderbookSnapshot | None = None       # HL-05 검증용


# ─────────────────────────── 결과 ───────────────────────────


@dataclass(frozen=True)
class Violation:
    rule_id: str
    rule_name: str
    reason: str


@dataclass(frozen=True)
class Decision:
    approved: bool
    violations: tuple[Violation, ...] = ()

    @classmethod
    def approve(cls) -> "Decision":
        return cls(True, ())

    @classmethod
    def reject(cls, *violations: Violation) -> "Decision":
        if not violations:
            raise ValueError("reject() requires at least one violation")
        return cls(False, violations)


# ─────────────────────────── 손절 추적 (HL-02) ───────────────────────────


class StopLossTracker:
    """연속 손절 카운터. 익절·리셋이 발생하면 0으로 초기화된다."""

    def __init__(self) -> None:
        self._stoploss_times: list[datetime] = []

    def record_stoploss(self, at: datetime) -> None:
        self._stoploss_times.append(at)

    def record_take_profit(self) -> None:
        self._stoploss_times.clear()

    def reset(self) -> None:
        self._stoploss_times.clear()

    @property
    def consecutive_count(self) -> int:
        return len(self._stoploss_times)

    @property
    def last_stoploss_at(self) -> datetime | None:
        return self._stoploss_times[-1] if self._stoploss_times else None

    def is_cooling_down(
        self,
        now: datetime,
        *,
        threshold: int,
        cooldown_minutes: int,
    ) -> bool:
        if self.consecutive_count < threshold:
            return False
        last = self.last_stoploss_at
        if last is None:
            return False
        return now < last + timedelta(minutes=cooldown_minutes)


# ─────────────────────────── 호가 틱 (HL-05) ───────────────────────────


def tick_size(price: int) -> int:
    """한국 주식 호가 단위 (KOSPI/KOSDAQ 공통 보수치)."""
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


# ─────────────────────────── 게이트 ───────────────────────────


class HardLimitGate:
    """§4 하드리밋 검증기. 모든 신규 진입은 본 gate를 통과해야 한다.

    청산(``OrderIntent.is_new_entry == False``) 주문은 진입 시간대(HL-03/04)
    와 연속 손절 쿨다운(HL-02), 동시 보유 제한(HL-01) 검증에서 면제된다.
    슬리피지(HL-05)와 신용 마진(HL-06)은 진입/청산 모두 적용된다.
    """

    def __init__(
        self,
        config: HardLimitsConfig,
        *,
        stoploss_tracker: StopLossTracker | None = None,
    ) -> None:
        self._cfg = config
        self._tracker = stoploss_tracker or StopLossTracker()

    @property
    def config(self) -> HardLimitsConfig:
        return self._cfg

    @property
    def tracker(self) -> StopLossTracker:
        return self._tracker

    def evaluate(self, intent: OrderIntent, ctx: MarketContext) -> Decision:
        violations: list[Violation] = []

        if intent.is_new_entry:
            for check in (
                self._check_blackout,
                self._check_concurrent_positions,
                self._check_stoploss_cooldown,
            ):
                v = check(intent, ctx)
                if v is not None:
                    violations.append(v)

        for check in (self._check_slippage, self._check_credit_margin):
            v = check(intent, ctx)
            if v is not None:
                violations.append(v)

        if violations:
            return Decision.reject(*violations)
        return Decision.approve()

    # ─── 룰 구현 ───

    def _check_blackout(
        self, intent: OrderIntent, ctx: MarketContext,
    ) -> Violation | None:
        """HL-03 / HL-04: 진입 금지 시간대 (14:30 이후 / 09:00~09:30)."""
        local_time = ctx.now.astimezone(KST).time()
        for window in self._cfg.entry_blackout_windows:
            if window.contains(local_time):
                rule_id = "HL-04" if window.start.hour < 12 else "HL-03"
                return Violation(
                    rule_id=rule_id,
                    rule_name=f"entry_blackout({window.start}-{window.end})",
                    reason=(
                        f"신규 진입 금지 시간대: {window.reason or '-'} "
                        f"(now={local_time.isoformat()})"
                    ),
                )
        return None

    def _check_concurrent_positions(
        self, intent: OrderIntent, ctx: MarketContext,
    ) -> Violation | None:
        """HL-01: 동시 보유 종목 수 ≤ 3. 기존 보유 종목 추가 매수는 카운트 X."""
        if intent.side != Side.BUY:
            return None
        held_codes = {p.code for p in ctx.balance.positions if p.qty > 0}
        if intent.code in held_codes:
            return None
        if len(held_codes) >= self._cfg.max_concurrent_positions:
            return Violation(
                rule_id="HL-01",
                rule_name="max_concurrent_positions",
                reason=(
                    f"동시 보유 종목 수 한도 초과: "
                    f"{len(held_codes)}/{self._cfg.max_concurrent_positions}"
                ),
            )
        return None

    def _check_stoploss_cooldown(
        self, intent: OrderIntent, ctx: MarketContext,
    ) -> Violation | None:
        """HL-02: 3연속 손절 시 1시간 신규 진입 금지."""
        del intent
        if not self._tracker.is_cooling_down(
            ctx.now,
            threshold=self._cfg.consecutive_stoploss_threshold,
            cooldown_minutes=self._cfg.cooldown_after_stoploss_minutes,
        ):
            return None
        last = self._tracker.last_stoploss_at
        assert last is not None
        unlock_at = last + timedelta(minutes=self._cfg.cooldown_after_stoploss_minutes)
        return Violation(
            rule_id="HL-02",
            rule_name="consecutive_stoploss_cooldown",
            reason=(
                f"{self._tracker.consecutive_count}연속 손절 쿨다운. "
                f"unlock_at={unlock_at.astimezone(KST).isoformat()}"
            ),
        )

    def _check_slippage(
        self, intent: OrderIntent, ctx: MarketContext,
    ) -> Violation | None:
        """HL-05: 호가 5틱 이상 슬리피지 예상 시 거절.

        - 시장가 주문(market)은 슬리피지를 사전 측정할 수 없어 본 룰 면제 (별도 가드 필요).
        - 호가 정보가 없거나 비어 있어도 면제 (정보부 응답 누락 시 진입 자체를 막진 않음).
        - 매수: ``ask[0]`` 대비 지정가가 5틱 이상 *낮으면* 호가 도달 전 슬리피지 위험.
        - 매도: ``bid[0]`` 대비 지정가가 5틱 이상 *높으면* 동일.
        """
        if intent.order_type != OrderType.LIMIT:
            return None
        ob = ctx.orderbook
        if ob is None or not ob.asks or not ob.bids:
            return None
        ref = ob.asks[0].price if intent.side == Side.BUY else ob.bids[0].price
        if ref <= 0:
            return None
        tick = tick_size(ref)
        max_dev = self._cfg.max_slippage_ticks * tick
        gap = ref - intent.price if intent.side == Side.BUY else intent.price - ref
        if gap > max_dev:
            return Violation(
                rule_id="HL-05",
                rule_name="max_slippage_ticks",
                reason=(
                    f"슬리피지 한도 초과: ref={ref}, intent={intent.price}, "
                    f"gap={gap}원 > {self._cfg.max_slippage_ticks} ticks × {tick}원"
                ),
            )
        return None

    def _check_credit_margin(
        self, intent: OrderIntent, ctx: MarketContext,
    ) -> Violation | None:
        """HL-06: 신용 매수 시 KIS 가용액에 추가 버퍼 적용.

        ``safe_limit = (maxBuyAmt - orderCashable) × (1 - buffer)``
        가 주문 금액보다 작으면 거절. 신용 매도는 본 룰 적용 X (포지션 청산이므로).
        """
        if not intent.use_credit:
            return None
        if intent.side != Side.BUY:
            return None
        oa = ctx.orderable_amount
        if oa is None:
            return Violation(
                rule_id="HL-06",
                rule_name="credit_margin_buffer",
                reason="신용 주문에 OrderableAmount 컨텍스트 필수",
            )
        order_value = intent.qty * intent.price
        credit_available = max(0, oa.maxBuyAmt - oa.orderCashable)
        buffer = self._cfg.margin_maintenance_buffer_pct
        safe_limit = int(credit_available * (1.0 - buffer))
        if order_value > safe_limit:
            return Violation(
                rule_id="HL-06",
                rule_name="credit_margin_buffer",
                reason=(
                    f"신용 주문 금액 {order_value:,} > 안전 한도 {safe_limit:,} "
                    f"(가용 {credit_available:,} × (1 - {buffer:.2%}))"
                ),
            )
        return None
