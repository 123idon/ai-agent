"""모의투자 가상 체결기 (CLAUDE.md §3.1).

실전 키로 시세/지표는 실제 수신하되, paper 모드에서는 **주문 실행만** 본 브로커로
가상 처리한다(KIS에 실제 주문을 보내지 않는다). 가상 현금/포지션을 추적하고
``state/paper_broker.json``에 영속화하여 재시작 후에도 모의 잔고가 유지된다.

체결 가정(v1):
- 지정가/시장가 모두 호출 시점에 전량 즉시 체결(미체결 없음).
- 수수료/세금 미반영(필요 시 확장).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .models import Side

log = logging.getLogger(__name__)


@dataclass
class PaperPosition:
    qty: int
    avg_price: float
    credit: float = 0.0      # 이 포지션에 묶인 신용(차입) 잔액 (§1.1 신용 적극 활용)


@dataclass
class PaperBroker:
    persist_path: Path | None = None
    start_cash: int = 100_000_000
    # 신용 포함 매수여력 배수. 1.0 = 신용 미사용(현금 전액 차감), 2.0 = 매수금액의
    # 절반만 현금에서 차감하고 나머지는 신용으로 충당(가용현금 × 2까지 매수 가능).
    credit_multiplier: float = 1.0
    cash: float = field(init=False)
    positions: dict[str, PaperPosition] = field(init=False)
    _seq: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self.cash = float(self.start_cash)
        self.positions = {}
        self._load()

    # ─────────────────────────── 영속화 ───────────────────────────

    def _load(self) -> None:
        if not self.persist_path or not self.persist_path.exists():
            return
        try:
            data = json.loads(self.persist_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("paper_broker 상태 로드 실패 — 초기 잔고로 시작")
            return
        self.cash = float(data.get("cash", self.start_cash))
        self._seq = int(data.get("seq", 0))
        self.positions = {
            code: PaperPosition(
                qty=int(p["qty"]), avg_price=float(p["avg_price"]),
                credit=float(p.get("credit", 0.0)),
            )
            for code, p in (data.get("positions") or {}).items()
            if int(p.get("qty", 0)) > 0
        }

    def reset(self, *, start_cash: int | None = None) -> None:
        """잔고를 초기 상태로 되돌린다 (백테스트 새 날짜 시작 시, §17).

        포지션을 비우고 현금을 ``start_cash``(미지정 시 생성 시 값)로 리셋한다.
        """
        if start_cash is not None:
            self.start_cash = int(start_cash)
        self.cash = float(self.start_cash)
        self.positions = {}
        self._seq = 0
        self.save()

    def save(self) -> None:
        if not self.persist_path:
            return
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            self.persist_path.write_text(
                json.dumps({
                    "cash": self.cash,
                    "seq": self._seq,
                    "positions": {
                        code: {
                            "qty": p.qty, "avg_price": p.avg_price,
                            "credit": p.credit,
                        }
                        for code, p in self.positions.items()
                    },
                }, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            log.warning("paper_broker 상태 저장 실패", exc_info=True)

    # ─────────────────────────── 체결 ───────────────────────────

    def fill(self, side: Side, code: str, qty: int, price: float) -> str:
        """가상 체결. 매수 시 현금 차감, 매도 시 현금 증가. 주문번호(가상) 반환."""
        if qty <= 0 or price <= 0:
            raise ValueError(f"invalid paper order qty={qty} price={price}")
        if side == Side.BUY:
            self._fill_buy(code, qty, price)
        else:
            self._fill_sell(code, qty, price)
        self._seq += 1
        self.save()
        ord_no = f"PAPER{self._seq:08d}"
        log.info(
            "PAPER_FILL %s %s qty=%d price=%.0f cash=%.0f",
            side.value, code, qty, price, self.cash,
        )
        return ord_no

    def _fill_buy(self, code: str, qty: int, price: float) -> None:
        cost = qty * price
        # 신용 분할: 매수금액의 (1/배수)만 현금에서 차감, 나머지는 신용으로 충당.
        # 배수 1.0 → 현금 전액 차감(신용 미사용), 2.0 → 절반은 신용(가용현금 50만 →
        # 100만원어치 매수 시 50만 현금차감 + 50만 신용, CLAUDE.md §1.1·요구 2).
        mult = self.credit_multiplier if self.credit_multiplier > 0 else 1.0
        cash_portion = cost / mult
        credit_portion = cost - cash_portion
        self.cash -= cash_portion
        pos = self.positions.get(code)
        if pos is None:
            self.positions[code] = PaperPosition(
                qty=qty, avg_price=price, credit=credit_portion,
            )
        else:
            total = pos.qty + qty
            pos.avg_price = (pos.avg_price * pos.qty + cost) / total
            pos.qty = total
            pos.credit += credit_portion

    def _fill_sell(self, code: str, qty: int, price: float) -> None:
        pos = self.positions.get(code)
        held = pos.qty if pos else 0
        sell_qty = min(qty, held)
        if sell_qty <= 0:
            return
        assert pos is not None
        # 매도 비율만큼 신용을 우선 상환하고 나머지를 현금으로 환수.
        portion = sell_qty / pos.qty
        credit_repay = pos.credit * portion
        self.cash += sell_qty * price - credit_repay
        pos.credit -= credit_repay
        pos.qty -= sell_qty
        if pos.qty <= 0:
            del self.positions[code]

    # ─────────────────────────── 조회 ───────────────────────────

    def orderable_cash(self) -> int:
        return int(max(0.0, self.cash))

    def credit_used(self) -> float:
        """전 포지션에 묶인 신용(차입) 총액."""
        return sum(p.credit for p in self.positions.values())

    def equity(self) -> float:
        """순자산(평가 미반영, 평단 기준): 현금 + 보유평가(평단) − 신용.

        현재가를 모르는 동기 호출용(일 시작 자산 추정 등). 정확한 평가는 시세를
        받는 ``ReplayKisClient.get_balance``가 산출한다.
        """
        held = sum(p.qty * p.avg_price for p in self.positions.values())
        return self.cash + held - self.credit_used()

    def snapshot(self) -> tuple[float, dict[str, PaperPosition]]:
        return self.cash, dict(self.positions)
