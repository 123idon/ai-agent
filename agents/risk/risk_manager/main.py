"""Risk manager agent (CLAUDE.md §2.4).

분석부의 ``EntrySignal``을 받아 사이징을 산출하고, ``HardLimitGate``를 통과하면
``risk.decision.approved``로, 거절되면 ``risk.decision.rejected``로 발행한다.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agents.analysis.signal.indicators import KST, Direction, Signal
from agents.analysis.signal.main import EntrySignal
from agents.intel.market_watch.main import MarketGrade
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
from core.notion_client import NotionKnowledgeView

log = logging.getLogger(__name__)

TOPIC_APPROVED = "risk.decision.approved"
TOPIC_REJECTED = "risk.decision.rejected"


@dataclass(frozen=True)
class SizingParams:
    """1차 진입 사이징 (CLAUDE.md §4.1 / §5 사이징 개정).

    매수금액 = 가용현금 × credit_multiplier × cash_fraction (신용 적극 활용).
    일봉 추세가 강할수록 비중을 높인다(요구):
      - STRONG 신호 + 일봉 강세 → 가용현금 × 2 × 0.7 (140만/100만 기준)
      - CONDITIONAL 신호       → 가용현금 × 2 × 0.4
      - STRONG 이나 일봉 미확인/약세 → 0.4로 보수화(일봉 강세 아닐 때 과대 진입 방지)
    """

    cash_fraction_strong: float = 0.7         # STRONG + 일봉 강세: 가용현금 × 2 × 0.7
    cash_fraction_conditional: float = 0.4    # CONDITIONAL: 가용현금 × 2 × 0.4
    credit_multiplier: float = 2.0            # 신용 포함 매수여력 배수 (가용현금 × 2)

    @classmethod
    def from_file(cls, path: "Path") -> "SizingParams":
        """strategy_params.yaml ``entry.sizing`` 에서 사이징 비중을 읽는다(읽기 전용).

        상담(💬)·회의(🤝)에서 비중을 바꾸면 다음 세션부터 즉시 반영된다(§25.3). 섹션이
        없으면 기본값을 쓴다(하위호환).
        """
        import yaml

        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        s = ((doc.get("entry") or {}).get("sizing")) or {}
        return cls(
            cash_fraction_strong=float(s.get("cash_fraction_strong", cls.cash_fraction_strong)),
            cash_fraction_conditional=float(
                s.get("cash_fraction_conditional", cls.cash_fraction_conditional)),
            credit_multiplier=float(s.get("credit_multiplier", cls.credit_multiplier)),
        )


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
        market_state_provider: Callable[[], MarketGrade] = lambda: MarketGrade.GREEN,
        grade_memory: Callable[[str], tuple[float | None, int]] | None = None,
        grade_winrate_floor: float = 35.0,
        grade_min_trades: int = 6,
        notion_knowledge: NotionKnowledgeView | None = None,
    ) -> None:
        self._kis = kis
        self._gate = gate
        self._bus = bus
        self._sizing = sizing or SizingParams()
        self._clock = clock
        self._market_state = market_state_provider
        # §19 메모리: 이 시장 등급에서 과거 승률이 낮으면 신규 진입 보류(참고 통계).
        self._grade_memory = grade_memory
        self._grade_floor = grade_winrate_floor
        self._grade_min = grade_min_trades
        # 학습부 노션 지식(세션 시작 시 참조) — 손절/익절·리스크 기준 카테고리.
        self._notion = notion_knowledge
        if notion_knowledge is not None and notion_knowledge.available:
            note = notion_knowledge.summary_line("risk.risk_manager")
            if note:
                log.info("📚 리스크 %s", note)

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

        grade = self._market_state()
        if grade == MarketGrade.BLACK:
            return await self._reject(signal, Violation(
                rule_id="MARKET_BLACK", rule_name="market_state",
                reason="market BLACK — 전 시스템 정지 권고",
            ))
        if grade == MarketGrade.RED:
            return await self._reject(signal, Violation(
                rule_id="MARKET_RED", rule_name="market_state",
                reason="market RED — 신규 진입 전면 금지",
            ))
        if grade == MarketGrade.YELLOW and signal.signal == Signal.CONDITIONAL_ENTRY:
            return await self._reject(signal, Violation(
                rule_id="MARKET_YELLOW_CONDITIONAL", rule_name="market_state",
                reason="market YELLOW — CONDITIONAL 진입 차단(§2.2.2)",
            ))

        # §19 장기 메모리: 이 시장 등급의 과거 승률이 낮고 표본이 충분하면 보류.
        # 단 GREEN(정상 운영, §2.2.2)은 기본 운영 등급이므로 메모리 사유로 전면
        # 거절하지 않는다 — GREEN을 막으면 시스템이 영구히 무거래로 자가정지(§5.7
        # "무보유→진입 반복" 위반)하는 죽음의 나선에 빠진다. 메모리 기반 등급 회피는
        # 위험 등급(YELLOW 등)에만 적용한다.
        if self._grade_memory is not None and grade != MarketGrade.GREEN:
            wr, n = self._grade_memory(grade.value)
            if wr is not None and n >= self._grade_min and wr < self._grade_floor:
                return await self._reject(signal, Violation(
                    rule_id="MEMORY_GRADE", rule_name="market_memory",
                    reason=f"이 시장 등급({grade.value}) 과거 승률 {wr:.0f}%·{n}회로 보류",
                ))

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
        # 일봉 추세 강할수록 비중 ↑ (§5 사이징 개정). STRONG이라도 일봉 강세가 아니면
        # CONDITIONAL 수준(0.4)으로 보수화한다.
        daily_strong = bool(getattr(signal, "daily_strong", False))
        if signal.signal == Signal.STRONG_ENTRY and daily_strong:
            frac = self._sizing.cash_fraction_strong
        elif signal.signal in (Signal.STRONG_ENTRY, Signal.CONDITIONAL_ENTRY):
            frac = self._sizing.cash_fraction_conditional
        else:
            return 0
        if signal.entry_price <= 0:
            return 0
        del orderable   # 매수여력은 가용현금 × 신용배수로 산출(브로커가 신용 분할 체결)
        cash = balance.cash
        # 매수금액 = 가용현금 × 비율 × 신용배수 (요구 2). 예: 100만 × 0.5 × 2 = 100만.
        buy_amount = int(cash * frac * self._sizing.credit_multiplier)
        qty = buy_amount // signal.entry_price
        # §4.1: 비율로는 1주도 못 사도 전체 매수여력(가용현금 × 신용배수)으로 1주
        # 이상 가능하면 집중 진입을 허용한다. 매수여력 < 1주 가격이면 0(SIZING 거절).
        if qty <= 0:
            buy_power = int(cash * self._sizing.credit_multiplier)
            if buy_power >= signal.entry_price:
                qty = buy_power // signal.entry_price
                log.info(
                    "SIZING 집중진입(§4.1): %s frac=%.0f%%로는 0주 → 매수여력 %d원으로 %d주",
                    signal.symbol, frac * 100, buy_power, qty,
                )
        return qty
