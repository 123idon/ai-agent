"""Unit tests for live exit rules (CLAUDE.md §5.3~5.5 / §5.7)."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path

from dataclasses import replace

from agents.execution.position_manager.exit_rules import (
    KST,
    CounterEffect,
    ExitKind,
    ExitParams,
    LivePositionState,
    evaluate_exit,
    select_tp_targets,
)

ROOT = Path(__file__).parents[2]


def _params() -> ExitParams:
    return ExitParams()  # 기본값 = strategy_params.yaml 하단값과 동일


def _now(h: int = 11, m: int = 0) -> datetime:
    return datetime(2026, 5, 29, h, m, 0, tzinfo=KST)


def _state(
    *,
    entry: float = 10_000,
    low: float = 9_900,
    qty: int = 10,
    high: float = 10_000,
    tp1: bool = False,
    tp2: bool = False,
    entry_time: datetime | None = None,
    tp1_target: float | None = None,
    tp2_target: float | None = None,
) -> LivePositionState:
    return LivePositionState(
        symbol="005930",
        entry_price=entry,
        entry_candle_low=low,
        qty_initial=qty,
        qty_open=qty,
        high_water=high,
        entry_time=entry_time or _now(),
        tp1_done=tp1,
        tp2_done=tp2,
        tp1_target=tp1_target,
        tp2_target=tp2_target,
    )


# ─────────────────────────── HOLD ───────────────────────────


def test_hold_when_nothing_triggers() -> None:
    a = evaluate_exit(_state(), price=10_050, now=_now(), breakdown_count=0, params=_params())
    assert a.kind == ExitKind.HOLD
    assert not a.is_exit
    assert a.counter == CounterEffect.NONE


# ─────────────────────────── 손절 ───────────────────────────


def test_hard_stop_at_minus_3pct() -> None:
    a = evaluate_exit(_state(), price=9_600, now=_now(), breakdown_count=0, params=_params())
    assert a.kind == ExitKind.HARD_STOP
    assert a.ratio == 1.0
    assert a.counter == CounterEffect.STOPLOSS


def test_technical_stop_below_entry_low() -> None:
    # 진입캔들 저점 -0.5%(9900×0.995=9850.5) 이탈, 하드(-3%=9700)에는 미도달
    a = evaluate_exit(_state(), price=9_840, now=_now(), breakdown_count=0, params=_params())
    assert a.kind == ExitKind.TECHNICAL
    assert a.counter == CounterEffect.STOPLOSS


def test_technical_stop_buffer_not_too_tight() -> None:
    # 저점(9900) 바로 아래(9890)는 -0.5% 버퍼(9850.5) 안이라 아직 손절 안 함(§5.4 개정).
    a = evaluate_exit(_state(), price=9_890, now=_now(), breakdown_count=0, params=_params())
    assert a.kind == ExitKind.HOLD


def test_hard_stop_priority_over_technical() -> None:
    # 둘 다 충족이면 하드가 먼저(우선순위 0번 EOD 제외하면 1번)
    a = evaluate_exit(_state(), price=9_500, now=_now(), breakdown_count=0, params=_params())
    assert a.kind == ExitKind.HARD_STOP


def test_signal_breakdown_removed_does_not_sell() -> None:
    # 신호붕괴 청산은 제거됨(§5.2): breakdown_count가 높아도 손절/익절 조건이
    # 아니면 보유 유지('신호 없이 파는' 조급한 청산 금지).
    state = _state(entry_time=_now(11, 0))
    a = evaluate_exit(state, price=10_010, now=_now(11, 6), breakdown_count=3, params=_params())
    assert a.kind == ExitKind.HOLD


def test_protective_stops_still_fire() -> None:
    # 하드 손절(-3%)·기술적 손절(진입캔들 저점 이탈)은 그대로 발동(보호 우선).
    state = _state(entry=10_000, low=9_900, entry_time=_now(11, 0))
    hard = evaluate_exit(state, price=9_600, now=_now(11, 1), breakdown_count=3, params=_params())
    assert hard.kind == ExitKind.HARD_STOP
    tech = evaluate_exit(state, price=9_850, now=_now(11, 1), breakdown_count=3, params=_params())
    assert tech.kind == ExitKind.TECHNICAL


def test_breakdown_count_never_triggers_exit() -> None:
    a = evaluate_exit(_state(), price=10_010, now=_now(), breakdown_count=5, params=_params())
    assert a.kind == ExitKind.HOLD


# ─────────────────────────── 익절 ───────────────────────────


def test_tp1_partial_close() -> None:
    a = evaluate_exit(_state(), price=10_300, now=_now(), breakdown_count=0, params=_params())
    assert a.kind == ExitKind.TP1
    assert a.ratio == 0.5                        # §5.3: 1차 50% 청산
    assert a.counter == CounterEffect.TAKEPROFIT


def test_tp2_after_tp1() -> None:
    a = evaluate_exit(
        _state(tp1=True), price=10_600, now=_now(), breakdown_count=0, params=_params(),
    )
    assert a.kind == ExitKind.TP2
    assert a.ratio == 0.3                        # §5.3: 2차 30% 청산
    assert a.counter == CounterEffect.TAKEPROFIT


def test_no_tp2_before_tp1() -> None:
    # tp1 미달성 상태에서 +6%면 (tp1 먼저) TP1로 처리
    a = evaluate_exit(_state(), price=10_600, now=_now(), breakdown_count=0, params=_params())
    assert a.kind == ExitKind.TP1


# ─────────────────────────── 트레일링 ───────────────────────────


def test_trailing_after_tp1() -> None:
    # tp1/tp2 완료, 고점 10800 대비 -2.04% 이탈(트레일링 -1.5%, §5.3)
    a = evaluate_exit(
        _state(tp1=True, tp2=True, high=10_800),
        price=10_580, now=_now(), breakdown_count=0, params=_params(),
    )
    assert a.kind == ExitKind.TRAILING
    assert a.counter == CounterEffect.TAKEPROFIT


def test_no_trailing_before_tp1() -> None:
    # tp1 전에는 트레일링 비활성. 고점 10200 대비 -1.96% 이탈이지만 ret 0% (TP1 미달)
    a = evaluate_exit(
        _state(high=10_200), price=10_000, now=_now(), breakdown_count=0, params=_params(),
    )
    assert a.kind == ExitKind.HOLD


# ─────────── 타임스톱 제거 검증 (시간 기반 매도 금지, §5.5) ───────────


def test_no_time_stop_after_long_hold() -> None:
    # 진입 후 오래(31분) 지나고 방향이 안 나도(횡보) 시간 때문에 파는 일은 없다 → HOLD.
    entry_time = _now() - timedelta(minutes=31)
    a = evaluate_exit(
        _state(entry_time=entry_time), price=10_010, now=_now(),
        breakdown_count=0, params=_params(),
    )
    assert a.kind == ExitKind.HOLD


def test_no_time_stop_even_when_flat_and_old() -> None:
    # 2시간 경과 + 미미한 손실(횡보)도 손절/EOD 전엔 청산하지 않는다(시간 트리거 없음).
    entry_time = _now() - timedelta(minutes=120)
    a = evaluate_exit(
        _state(entry_time=entry_time), price=9_980, now=_now(),
        breakdown_count=0, params=_params(),
    )
    assert a.kind == ExitKind.HOLD


def test_exit_params_has_no_time_stop_fields() -> None:
    # 타임스톱 파라미터는 ExitParams에서 완전히 제거되었다.
    p = _params()
    for attr in (
        "time_stop_enabled", "time_stop_minutes", "time_stop_action",
        "time_stop_first_minutes", "flat_box_pct",
    ):
        assert not hasattr(p, attr), f"{attr} 가 아직 남아있음"


def test_technical_stop_disabled() -> None:
    # technical_stop_enabled=False → use_technical_stop False → 기술적 손절 미발동(하드만)
    p = replace(_params(), use_technical_stop=False, technical_stop_enabled=False)
    a = evaluate_exit(_state(), price=9_750, now=_now(), breakdown_count=0, params=p)
    assert a.kind == ExitKind.HOLD   # 9750은 하드(-3%=9700) 미도달, 기술적 꺼짐 → 보유


# ─────────────────────────── EOD ───────────────────────────


def test_eod_force_close() -> None:
    a = evaluate_exit(_state(), price=10_050, now=_now(15, 26), breakdown_count=0, params=_params())
    assert a.kind == ExitKind.EOD_FORCE
    assert a.ratio == 1.0
    assert a.counter == CounterEffect.NEUTRAL


def test_eod_force_close_exactly_1520() -> None:
    # §5.7: 15:20 정각에 무조건 전량 강제청산 (어떤 조건에서도 예외 없음).
    a = evaluate_exit(_state(), price=10_050, now=_now(15, 20), breakdown_count=0, params=_params())
    assert a.kind == ExitKind.EOD_FORCE
    assert a.ratio == 1.0
    assert "EOD 강제청산" in a.reason


def test_eod_force_close_no_tp_no_exception() -> None:
    # 익절·손절·트레일링 어느 것도 트리거되지 않은 평범한 보유분도 15:20엔 무조건 청산.
    a = evaluate_exit(
        _state(tp1=False, high=10_000), price=10_010, now=_now(15, 22),
        breakdown_count=0, params=_params(),
    )
    assert a.kind == ExitKind.EOD_FORCE
    assert a.ratio == 1.0


def test_eod_priority_over_profit() -> None:
    # 마감 강제는 익절보다 우선
    a = evaluate_exit(_state(), price=10_400, now=_now(15, 30), breakdown_count=0, params=_params())
    assert a.kind == ExitKind.EOD_FORCE


def test_trailing_cutoff_closes_remainder() -> None:
    # 15:20 이후, tp1 완료 + 트레일링 미발동(고점 근처)이면 EOD 강제청산으로 잔여 전량 청산
    a = evaluate_exit(
        _state(tp1=True, high=10_500), price=10_490, now=_now(15, 21),
        breakdown_count=0, params=_params(),
    )
    assert a.kind == ExitKind.EOD_FORCE


# ─────────────────────────── config 파싱 ───────────────────────────


# ─────────────────────────── ATR 동적 익절 (§5.3) ───────────────────────────


def test_select_tp_targets_banding() -> None:
    p = _params()
    assert select_tp_targets(0.010, p) == (p.tp1_low, p.tp2_low)         # 저변동성 → 3/6%
    assert select_tp_targets(0.020, p) == (
        (p.tp1_low + p.tp1_high) / 2, (p.tp2_low + p.tp2_high) / 2,      # 중 → 4/7%
    )
    assert select_tp_targets(0.040, p) == (p.tp1_high, p.tp2_high)       # 고 → 5/8%
    assert select_tp_targets(None, p) == (p.tp1_low, p.tp2_low)          # 미상 → 하단 폴백


def test_dynamic_tp1_high_atr_suppresses_early_exit() -> None:
    # 고변동성으로 TP1 목표가가 +5%로 사전 지정된 포지션은 +3.5%에서 익절하지 않음
    st = _state(tp1_target=0.05, tp2_target=0.08)
    assert evaluate_exit(st, price=10_350, now=_now(), breakdown_count=0, params=_params()).kind == ExitKind.HOLD
    # +5% 도달 시 TP1
    a = evaluate_exit(st, price=10_500, now=_now(), breakdown_count=0, params=_params())
    assert a.kind == ExitKind.TP1


def test_dynamic_tp2_uses_target() -> None:
    st = _state(tp1=True, tp1_target=0.05, tp2_target=0.08)
    # +7% 에서는 (목표 8%) 아직 TP2 아님 → HOLD
    assert evaluate_exit(st, price=10_700, now=_now(), breakdown_count=0, params=_params()).kind == ExitKind.HOLD
    a = evaluate_exit(st, price=10_800, now=_now(), breakdown_count=0, params=_params())
    assert a.kind == ExitKind.TP2


def test_from_file_reads_strategy_params() -> None:
    p = ExitParams.from_file(ROOT / "config" / "strategy_params.yaml")
    assert p.tp1_pct == 0.03
    assert p.tp1_close_ratio == 0.5                 # §5.3: 1차 50%
    assert p.tp2_pct == 0.06
    assert p.tp2_high == 0.08                        # +6~8%
    assert p.tp2_close_ratio == 0.3                 # §5.3: 2차 30%
    assert p.trailing_pct == 0.015                   # 고점 -1.5%
    assert p.hard_stop_pct == -0.03                  # 최대 손절 -3%
    assert p.technical_buffer_pct == 0.005           # 진입캔들 저점 -0.5%
    assert p.use_technical_stop is True
    assert p.technical_stop_enabled is True          # 요구 1: yaml에서 읽음
    assert p.cutoff_time == time(15, 20, 0)
    assert p.force_close_time == time(15, 20, 0)


def test_strategy_params_has_no_time_stop_section() -> None:
    # 타임스톱(시간 기반 매도)은 제거되어 yaml에 time_stop 설정이 없어야 한다(§5.5).
    import yaml
    _doc = yaml.safe_load((ROOT / "config" / "strategy_params.yaml").read_text(encoding="utf-8"))
    assert "time_stop" not in _doc
