"""Order execution agent (CLAUDE.md §2.5).

리스크부의 ``ApprovedOrder``를 받아 KisClient로 송신한다. 현금/신용은
``ApprovedOrder.use_credit``으로 분기하며, 모드(paper/live)는 KisClient가
자체 관리한다(별도 라우터 불요).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from agents.analysis.signal.indicators import KST
from agents.risk.risk_manager.main import ApprovedOrder
from core.kis_client import (
    KisAuthError,
    KisBusinessError,
    KisClient,
    KisTransportError,
    Mode,
    Side,
)
from core.messaging import Bus

log = logging.getLogger(__name__)

TOPIC_EVENT = "order.event"
TOPIC_FAILED = "order.failed"


@dataclass(frozen=True)
class OrderEvent:
    """주문 송신 성공. 학습부 journal + CEO 보고 대상."""

    ord_no: str
    symbol: str
    side: Side
    qty: int
    price: int
    use_credit: bool
    mode: Mode
    timestamp: datetime
    msg: str | None
    approved: ApprovedOrder


@dataclass(frozen=True)
class OrderFailed:
    symbol: str
    error: str
    approved: ApprovedOrder
    timestamp: datetime


class OrderAgent:
    def __init__(
        self,
        kis: KisClient,
        bus: Bus,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(KST),
    ) -> None:
        self._kis = kis
        self._bus = bus
        self._clock = clock

    async def execute(self, order: ApprovedOrder) -> OrderEvent | OrderFailed:
        try:
            if order.use_credit:
                result = await self._kis.place_credit_order(
                    side=order.side,
                    code=order.code,
                    qty=order.qty,
                    price=order.price,
                    order_type=order.order_type,
                )
            else:
                result = await self._kis.place_order(
                    side=order.side,
                    code=order.code,
                    qty=order.qty,
                    price=order.price,
                    order_type=order.order_type,
                )
        except (KisBusinessError, KisAuthError, KisTransportError) as e:
            failed = OrderFailed(
                symbol=order.symbol,
                error=f"{type(e).__name__}: {e}",
                approved=order,
                timestamp=self._clock(),
            )
            log.error("ORDER_FAILED %s: %s", order.symbol, e)
            await self._bus.publish(TOPIC_FAILED, failed)
            return failed

        event = OrderEvent(
            ord_no=result.ordNo,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            price=order.price,
            use_credit=order.use_credit,
            mode=self._kis.mode,
            timestamp=self._clock(),
            msg=result.msg,
            approved=order,
        )
        log.info("ORDER_OK %s ord_no=%s", order.symbol, result.ordNo)
        await self._bus.publish(TOPIC_EVENT, event)
        # 신용 매수 성공 시 ledger 동기화 시도 (실패는 비치명)
        if order.use_credit and order.side == Side.BUY:
            try:
                await self._kis.sync_credit_ledger()
            except Exception:
                log.warning("credit ledger sync failed", exc_info=True)
        return event
