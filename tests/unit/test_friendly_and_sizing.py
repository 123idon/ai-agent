"""매매 사유 친화 변환(요구 1) + 일봉 강세 기반 사이징(요구 2) 단위 테스트."""
from __future__ import annotations

from datetime import datetime, timezone

from agents.analysis.signal.indicators import Direction, Signal
from agents.analysis.signal.main import EntrySignal
from agents.execution.position_manager.exit_rules import ExitKind, friendly_exit_reason
from agents.risk.risk_manager.main import RiskAgent, SizingParams
from core.kis_client import BalanceSnapshot

KST = timezone(__import__("datetime").timedelta(hours=9))


# ─────────────────────────── 친화 사유 (요구 1) ───────────────────────────


def test_friendly_technical_matches_example() -> None:
    msg = friendly_exit_reason(ExitKind.TECHNICAL, -0.0111, ratio=1.0)
    assert msg == "📉 진입할 때 저점 밑으로 떨어져서 손절했어요 (-1.11%)"


def test_no_time_stop_kind_exists() -> None:
    # 타임스톱(시간 기반 매도)은 제거되어 ExitKind에 TIME_STOP 종류가 없어야 한다(§5.5).
    names = {k.name for k in ExitKind}
    assert "TIME_STOP" not in names
    assert "TIME_STOP_FIRST" not in names


def test_friendly_covers_all_kinds() -> None:
    for kind in ExitKind:
        msg = friendly_exit_reason(kind, 0.012, ratio=0.4)
        assert isinstance(msg, str) and msg
        if kind not in (ExitKind.HOLD,):
            assert "%" in msg                 # 손익률 포함


def test_friendly_portion_words() -> None:
    assert "전량" in friendly_exit_reason(ExitKind.HARD_STOP, -0.02, ratio=1.0)
    assert "절반" in friendly_exit_reason(ExitKind.SIGNAL_BREAKDOWN, -0.001, ratio=0.5)
    assert "40%" in friendly_exit_reason(ExitKind.TP1, 0.03, ratio=0.4)


# ─────────────────────────── 사이징 (요구 2) ───────────────────────────


def _sig(kind: Signal, *, daily_strong: bool) -> EntrySignal:
    return EntrySignal(
        symbol="005930", direction=Direction.LONG, signal=kind, score_count=4,
        entry_price=10_000, entry_candle_low=9_900, entry_candle_high=10_100,
        use_credit_hint=False, timestamp=datetime(2026, 5, 29, 10, 30, tzinfo=KST),
        reason="t", daily_strong=daily_strong,
    )


def _balance(cash: int) -> BalanceSnapshot:
    return BalanceSnapshot(cash=cash, totalEval=cash, totalPnl=0, positions=[])


def _agent() -> RiskAgent:
    # _size는 kis/gate/bus를 쓰지 않으므로 더미로 충분.
    return RiskAgent(object(), object(), object(), sizing=SizingParams())


def test_strong_with_daily_strong_uses_0_7() -> None:
    qty = _agent()._size(_sig(Signal.STRONG_ENTRY, daily_strong=True), _balance(1_000_000), None)
    # 100만 × 2 × 0.7 = 140만 / 10_000 = 140주
    assert qty == int(1_000_000 * 2 * 0.7) // 10_000


def test_strong_without_daily_strong_is_conservative_0_4() -> None:
    qty = _agent()._size(_sig(Signal.STRONG_ENTRY, daily_strong=False), _balance(1_000_000), None)
    # 일봉 강세 아님 → 0.4로 보수화: 100만 × 2 × 0.4 = 80만 / 10_000 = 80주
    assert qty == int(1_000_000 * 2 * 0.4) // 10_000


def test_conditional_uses_0_4() -> None:
    qty = _agent()._size(_sig(Signal.CONDITIONAL_ENTRY, daily_strong=True), _balance(1_000_000), None)
    assert qty == int(1_000_000 * 2 * 0.4) // 10_000
