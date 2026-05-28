"""Risk manager agent (CLAUDE.md §2.4).

분석부의 ``EntrySignal``을 받아 사이징을 산출하고, ``HardLimitGate``를 통과하면
``risk.decision.approved``로, 거절되면 ``risk.decision.rejected``로 발행한다.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from agents.analysis.signal.indicators import KST, Direction, Signal
from agents.analysis.signal.main import EntrySignal
from agents.risk.risk_manager.hard_limits import (
    HardLimitGate,
    MarketContext,
    OrderIntent,
    Violation,
)
from core.kis_client import (
    BalanceSnapshot,
    KisBusinessError,
    KisClient,
    OrderableAmount,
    OrderType,
    OrderbookSnapshot,
    Side,
)
from core.messaging import Bus

log = logging.getLogger(__name__)

TOPIC_APPROVED = "risk.decision.approved"
TOPIC_REJECTED = "risk.decision.rejected"


@dataclass(frozen=True)
class SizingParams:
    """1차 진입 트랜치 사이징. 분할매수의 첫 단계만 본 모듈에서 결정한다."""

    base_pct_strong: float = 0.30
    base_pct_conditional: float = 0.20


@dataclass(frozen=True)
class ApprovedOrder:
    """리스크부 → 실행부 페이로드."""

    symbol: str
    side: Side
    code: str
    qty: int
    price: int
    order_type: OrderType
    use_credit: bool
    is_new_entry: bool
    entry_signal: EntrySignal       # 원 신호 (학습부 trace)
    timestamp: datetime
    reason: str


@dataclass(frozen=True)
class RejectedOrder:
    symbol: str
    violations: tuple[Violation, ...]
    entry_signal: EntrySignal
    timestamp: datetime


class RiskAgent:
    def __init__(
        self,
        kis: KisClient,
        gate: HardLimitGate,
        bus: Bus,
        *,
        sizing: SizingParams | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(KST),
    ) -> None:
        self._kis = kis
        self._gate = gate
        self._bus = bus
        self._sizing = sizing or SizingParams()
        self._clock = clock

    async def review(self, signal: EntrySignal) -> ApprovedOrder | RejectedOrder:
        if signal.direction != Direction.LONG:
            return await self._reject(
                signal,
                Violation(
                    rule_id="UNSUPPORTED",
                    rule_name="direction",
                    reason="short direction not supported in v1",
                ),
            )

        balance = await self._kis.get_balance()
        orderbook = await self._safe_orderbook(signal.symbol)
        orderable = await self._safe_orderable(signal) if signal.use_credit_hint else None

        qty = self._size(signal, balance, orderable)
        if qty <= 0:
            return await self._reject(
                signal,
                Violation(
                    rule_id="SIZING",
                    rule_name="qty",
                    reason="가용액 부족 또는 진입가 0",
                ),
            )

        intent = OrderIntent(
            side=Side.BUY,
            code=signal.symbol,
            qty=qty,
            price=signal.entry_price,
            order_type=OrderType.LIMIT,
            use_credit=signal.use_credit_hint,
            is_new_entry=True,
        )
        ctx = MarketContext(
            now=self._clock(),
            balance=balance,
            orderbook=orderbook,
            orderable_amount=orderable,
        )
        decision = self._gate.evaluate(intent, ctx)
        if not decision.approved:
            return await self._reject(signal, *decision.violations)

        approved = ApprovedOrder(
            symbol=signal.symbol,
            side=Side.BUY,
            code=signal.symbol,
            qty=qty,
            price=signal.entry_price,
            order_type=OrderType.LIMIT,
            use_credit=signal.use_credit_hint,
            is_new_entry=True,
            entry_signal=signal,
            timestamp=ctx.now,
            reason=signal.reason,
        )
        log.info(
            "APPROVE %s qty=%d price=%d credit=%s",
            signal.symbol, qty, signal.entry_price, signal.use_credit_hint,
        )
        await self._bus.publish(TOPIC_APPROVED, approved)
        return approved

    async def _reject(
        self, signal: EntrySignal, *violations: Violation,
    ) -> RejectedOrder:
        rejected = RejectedOrder(
            symbol=signal.symbol,
            violations=tuple(violations),
            entry_signal=signal,
            timestamp=self._clock(),
        )
        log.info(
            "REJECT %s: %s",
            signal.symbol,
            "; ".join(f"{v.rule_id}:{v.reason}" for v in violations),
        )
        await self._bus.publish(TOPIC_REJECTED, rejected)
        return rejected

    async def _safe_orderbook(self, code: str) -> OrderbookSnapshot | None:
        try:
            return await self._kis.get_orderbook(code)
        except (KisBusinessError, Exception) as e:  # noqa: BLE001
            log.warning("orderbook fetch failed for %s: %s", code, e)
            return None

    async def _safe_orderable(self, signal: EntrySignal) -> OrderableAmount | None:
        try:
            return await self._kis.get_orderable_amount(
                code=signal.symbol, price=signal.entry_price,
            )
        except (KisBusinessError, Exception) as e:  # noqa: BLE001
            log.warning("orderable fetch failed for %s: %s", signal.symbol, e)
            return None

    def _size(
        self,
        signal: EntrySignal,
        balance: BalanceSnapshot,
        orderable: OrderableAmount | None,
    ) -> int:
        if signal.signal == Signal.STRONG_ENTRY:
            pct = self._sizing.base_pct_strong
        elif signal.signal == Signal.CONDITIONAL_ENTRY:
            pct = self._sizing.base_pct_conditional
        else:
            return 0
        if signal.entry_price <= 0:
            return 0
        if signal.use_credit_hint and orderable is not None:
            available = orderable.maxBuyAmt
        else:
            available = balance.cash
        target_value = int(available * pct)
        return target_value // signal.entry_price
