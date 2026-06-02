"""복기 패턴 추출 → 전략 개선안 (CLAUDE.md §2.6, §11).

저널의 ``signal.exit`` 흐름을 훑어 **반복되는 손실 패턴**을 찾아 구체적 파라미터
개선안(``ReviewSuggestion``)을 만든다. 메타부 제안과 달리, 운영자가 말로 표현하던
복기 규칙을 코드화한 것이다:

  - 손절 3번 연속 → 진입 기준 RSI 범위 좁히기(하단 +5)
  - 하드 손절(-N%) 빈발 → (기술적 손절이 늦음) 유예 단축

타임스톱(시간 기반 매도)은 제거되어(§5.5) 관련 복기 규칙도 더 이상 없다.

순수 함수(저널 레코드 + 현재값 getter)라 단위 테스트가 쉽다. 적용 여부는 호출자
(``scripts/auto_learn.py``)가 ``StrategyEditor`` 로 결정한다.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

_STOPLOSS_KINDS = {"hard_stop_loss", "technical_stop", "signal_breakdown"}


@dataclass(frozen=True)
class ReviewSuggestion:
    key: str
    from_value: Any
    to_value: Any
    reason: str
    expected_effect: str
    evidence: dict[str, Any]


class ReviewLearner:
    def __init__(
        self,
        current_value: Callable[[str], Any],
        *,
        min_trades: int = 5,
        consecutive_stoploss_floor: int = 3,
        rsi_low_cap: float = 60.0,
    ) -> None:
        self._cur = current_value
        self._min_trades = min_trades
        self._sl_floor = consecutive_stoploss_floor
        self._rsi_low_cap = rsi_low_cap

    def analyze(self, records: Sequence[dict[str, Any]]) -> list[ReviewSuggestion]:
        exits = _exits_in_order(records)
        if len(exits) < self._min_trades:
            return []

        out: list[ReviewSuggestion] = []
        out += self._rule_consecutive_stoploss(exits)
        # 중복 키 제거(앞선 규칙 우선)
        seen: set[str] = set()
        deduped: list[ReviewSuggestion] = []
        for s in out:
            if s.key in seen:
                continue
            seen.add(s.key)
            deduped.append(s)
        return deduped

    # ── 규칙 A: 손절 연속 → RSI 진입 구간 좁히기 ──

    def _rule_consecutive_stoploss(
        self, exits: list[dict[str, Any]],
    ) -> list[ReviewSuggestion]:
        run = best = 0
        for e in exits:
            if e["kind"] in _STOPLOSS_KINDS and e["pnl"] < 0:
                run += 1
                best = max(best, run)
            else:
                run = 0
        if best < self._sl_floor:
            return []
        cur = self._cur("signal.rsi.entry_zone")
        if not isinstance(cur, (list, tuple)) or len(cur) != 2:
            return []
        lo, hi = float(cur[0]), float(cur[1])
        new_lo = min(lo + 5, self._rsi_low_cap, hi - 5)
        if new_lo <= lo:
            return []
        return [ReviewSuggestion(
            key="signal.rsi.entry_zone",
            from_value=[int(lo), int(hi)],
            to_value=[int(new_lo), int(hi)],
            reason=f"손절 {best}회 연속 — 진입 RSI 하단을 {int(lo)}→{int(new_lo)}로 좁혀 질 개선",
            expected_effect="더 강한 모멘텀에서만 진입 → 연속 손절 감소 기대",
            evidence={"max_consecutive_stoploss": best},
        )]


def _exits_in_order(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(records, key=lambda r: str(r.get("ts", "")))
    out: list[dict[str, Any]] = []
    for rec in ordered:
        if rec.get("topic") != "signal.exit":
            continue
        p = rec.get("payload")
        if not isinstance(p, dict):
            continue
        out.append({
            "kind": str(p.get("kind", "")),
            "pnl": float(p.get("pnl_pct", 0.0)),
        })
    return out
