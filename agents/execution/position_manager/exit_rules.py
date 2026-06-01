"""실시간 단일 포지션 청산 규칙 (CLAUDE.md §5.3~5.5 / §5.7).

``agents/learning/pattern/backtest.py``의 ``_maybe_exit`` 규칙을 실거래 단일
포지션용으로 이식하되 **3단 부분청산**(TP1 50% → TP2 30% → 트레일링 잔여)을
실제로 지원한다. 본 모듈은 순수 함수/데이터로만 구성되어 KisClient·Bus 의존이
없으므로 단위 테스트가 용이하다.

청산 파라미터는 ``config/strategy_params.yaml``의 take_profit/stop_loss/time_stop
에서만 읽는다. 이 파일은 live 모드에서 SHA-256 해시 잠금(§3.3) 대상이므로
**읽기 전용**으로만 접근한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from pathlib import Path

import yaml

KST = timezone(timedelta(hours=9))


# ─────────────────────────── enums ───────────────────────────


class ExitKind(str, Enum):
    HOLD = "hold"
    TP1 = "take_profit_1"
    TP2 = "take_profit_2"
    TRAILING = "trailing_stop"
    HARD_STOP = "hard_stop_loss"
    TECHNICAL = "technical_stop"
    SIGNAL_BREAKDOWN = "signal_breakdown"
    TIME_STOP = "time_stop"
    TIME_STOP_FIRST = "time_stop_first"   # 1차 타임스톱 체크(§5.5 2단 구조)
    EOD_FORCE = "eod_force_close"


class CounterEffect(str, Enum):
    """HL-02 연속손절 카운터에 미치는 영향 (§5.4 / §5.5)."""

    NONE = "none"             # HOLD
    STOPLOSS = "stoploss"     # 하드/기술적/신호붕괴 → record_stoploss
    TAKEPROFIT = "takeprofit" # 익절/트레일링 → record_take_profit(리셋)
    NEUTRAL = "neutral"       # 타임스톱/EOD → 카운터 미산입


_STOPLOSS_KINDS = frozenset(
    {ExitKind.HARD_STOP, ExitKind.TECHNICAL, ExitKind.SIGNAL_BREAKDOWN}
)
_TAKEPROFIT_KINDS = frozenset({ExitKind.TP1, ExitKind.TP2, ExitKind.TRAILING})


def _counter_effect(kind: ExitKind) -> CounterEffect:
    if kind in _STOPLOSS_KINDS:
        return CounterEffect.STOPLOSS
    if kind in _TAKEPROFIT_KINDS:
        return CounterEffect.TAKEPROFIT
    if kind == ExitKind.HOLD:
        return CounterEffect.NONE
    return CounterEffect.NEUTRAL


# 타임스톱 동작 문자열 → 청산 비율. hold=0.0(청산 안 함), reduce_50=절반, exit_all=전량.
_ACTION_RATIO: dict[str, float] = {"hold": 0.0, "reduce_50": 0.5, "exit_all": 1.0}


def action_ratio(action: str) -> float:
    """타임스톱 action 문자열을 청산 비율로. 알 수 없으면 보수적으로 0.5(절반)."""
    return _ACTION_RATIO.get(str(action).strip().lower(), 0.5)


# ─────────────────────────── config ───────────────────────────


@dataclass(frozen=True)
class ExitParams:
    """strategy_params.yaml에서 파생된 청산 파라미터 (읽기 전용)."""

    # 익절 목표가 범위 (§5.3): ATR에 따라 [low, high] 사이에서 동적 선택.
    # tp1_pct/tp2_pct는 ATR을 알 수 없을 때의 폴백(범위 하단). 최소 목표 상향(§5.3 개정):
    # 1차 +3~5% / 40% 청산, 2차 +6~10% / 40% 청산.
    tp1_low: float = 0.03
    tp1_high: float = 0.05
    tp1_pct: float = 0.03
    tp1_close_ratio: float = 0.4
    tp2_low: float = 0.06
    tp2_high: float = 0.10
    tp2_pct: float = 0.06
    tp2_close_ratio: float = 0.4
    # ATR% 밴딩 경계: < low → 범위 하단, < high → 범위 중앙, ≥ high → 범위 상단
    atr_band_low: float = 0.015
    atr_band_high: float = 0.030
    trailing_pct: float = 0.02            # 고점 대비 -2% 이탈 시 잔량 전량(§5.3 개정)
    hard_stop_pct: float = -0.02          # 최대 손절 -2%(기존 -3%에서 축소, §5.4 개정)
    use_technical_stop: bool = True       # entry_candle_low_breach AND technical_stop_enabled
    technical_stop_enabled: bool = True   # 기술적 손절 on/off (consult/회의로 토글)
    # 기술적 손절은 진입캔들 저점에서 추가로 이 비율만큼 더 내려야 발동(너무 빡빡하지 않게, §5.4 개정).
    technical_buffer_pct: float = 0.005   # 진입캔들 저점 -0.5% 이탈 시
    signal_breakdown_min_count: int = 2
    # §5.6 평균 보유 30분~3시간. 진입 직후 1분봉 노이즈(거래량 자연 감소·5MA 일시 이탈)로
    # "신호붕괴"가 즉시 발동해 포지션이 1분 만에 청산되는 것을 막는 유예 시간(분).
    # 하드 손절(-3%)·기술적 손절(진입캔들 저점 이탈)·EOD는 유예와 무관하게 항상 발동한다.
    signal_breakdown_grace_minutes: int = 5
    # 타임스톱(§5.5 개정, 2단 체크): 메인 체크 N분 내 +1% 미달 시 action(reduce_50/exit_all).
    # 1차 체크(first_*)는 메인 전 구간에서 1회 평가하며 hold면 보류. flat_box_pct는 폐기.
    time_stop_enabled: bool = True
    time_stop_minutes: int = 30           # 메인 체크 경과 시간(분)
    flat_box_pct: float = 0.005           # (구) ±박스 — 호환용, 미사용
    time_stop_min_profit: float = 0.01    # 메인 체크: 경과 시 ret < +1%면 action 발동
    time_stop_action: str = "reduce_50"   # 메인 체크 동작: reduce_50 | exit_all
    time_stop_first_minutes: int = 0      # 1차 체크 시간(분), 0 = 비활성
    time_stop_first_action: str = "hold"  # 1차 체크 동작: hold | reduce_50 | exit_all
    time_stop_first_min_profit: float = 0.0  # 1차 체크 기준 수익(미달 시 first action)
    # §5.7 EOD 강제청산: 당일 매수분은 어떤 조건에서도 예외 없이 15:20에 전량 청산한다.
    # cutoff_time(트레일링 종료)과 force_close_time(전량 강제청산)을 15:20으로 일치시켜
    # 보유 종목이 다음 거래일로 이월되지 않도록 보장한다.
    cutoff_time: time = time(15, 20, 0)        # 트레일링 종료
    force_close_time: time = time(15, 20, 0)   # 보유분 전량 강제청산 (예외 없음)

    @classmethod
    def from_file(cls, path: Path) -> "ExitParams":
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        tp = doc.get("take_profit", {})
        step1 = tp.get("step1", {})
        step2 = tp.get("step2", {})
        step3 = tp.get("step3_trailing", {})
        sl = doc.get("stop_loss", {})
        ts = doc.get("time_stop", {})

        # §5.3: 익절 목표가는 ATR에 따라 pct_range 안에서 동적 선택된다.
        tp1_range = step1.get("pct_range", [0.03, 0.05])
        tp2_range = step2.get("pct_range", [0.06, 0.10])
        atr_band = tp.get("atr_bands", {})

        return cls(
            tp1_low=float(tp1_range[0]),
            tp1_high=float(tp1_range[-1]),
            tp1_pct=float(tp1_range[0]),
            tp1_close_ratio=float(step1.get("close_ratio", 0.4)),
            tp2_low=float(tp2_range[0]),
            tp2_high=float(tp2_range[-1]),
            tp2_pct=float(tp2_range[0]),
            tp2_close_ratio=float(step2.get("close_ratio", 0.4)),
            atr_band_low=float(atr_band.get("low", 0.015)),
            atr_band_high=float(atr_band.get("high", 0.030)),
            trailing_pct=float(step3.get("trail_from_high_pct", 0.02)),
            hard_stop_pct=float(sl.get("hard_max_pct", -0.02)),
            use_technical_stop=bool(sl.get("entry_candle_low_breach", True))
            and bool(sl.get("technical_stop_enabled", True)),
            technical_stop_enabled=bool(sl.get("technical_stop_enabled", True)),
            technical_buffer_pct=float(sl.get("technical_buffer_pct", 0.005)),
            signal_breakdown_min_count=int(sl.get("signal_breakdown_min_count", 2)),
            signal_breakdown_grace_minutes=int(sl.get("signal_breakdown_grace_minutes", 5)),
            time_stop_enabled=bool(ts.get("enabled", True)),
            time_stop_minutes=int(ts.get("evaluation_minutes", 30)),
            flat_box_pct=float(ts.get("flat_box_pct", 0.005)),
            time_stop_min_profit=float(ts.get("min_profit_pct", 0.01)),
            time_stop_action=str(ts.get("action", "reduce_50")),
            time_stop_first_minutes=int(ts.get("first_check_minutes", 0)),
            time_stop_first_action=str(ts.get("first_check_action", "hold")),
            time_stop_first_min_profit=float(ts.get("first_check_min_profit_pct", 0.0)),
            cutoff_time=_parse_time(step3.get("cutoff_time", "15:20:00")),
            force_close_time=_parse_time(step3.get("force_close_time", "15:20:00")),
        )


def select_tp_targets(
    atr_pct: float | None, params: ExitParams,
) -> tuple[float, float]:
    """ATR%에 따라 익절 1·2차 목표가를 범위 안에서 동적 선택 (§5.3).

    - 저변동성(ATR% < atr_band_low)  → 범위 하단 (예: +3% / +6%): 빨리 차익 실현
    - 중변동성(< atr_band_high)       → 범위 중앙 (예: +4% / +7%)
    - 고변동성(≥ atr_band_high)       → 범위 상단 (예: +5% / +8%): 더 멀리 둠
    - ATR% 미상(None)                 → 범위 하단(보수적 폴백)
    """
    if atr_pct is None:
        return params.tp1_low, params.tp2_low
    if atr_pct < params.atr_band_low:
        return params.tp1_low, params.tp2_low
    if atr_pct < params.atr_band_high:
        return (params.tp1_low + params.tp1_high) / 2.0, (params.tp2_low + params.tp2_high) / 2.0
    return params.tp1_high, params.tp2_high


def _parse_time(value: str | time) -> time:
    if isinstance(value, time):
        return value
    parts = str(value).split(":")
    return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)


# ─────────────────────────── state / action ───────────────────────────


@dataclass
class LivePositionState:
    """보유 중 단일 포지션의 런타임 상태 (KIS 잔고로 권위 보정)."""

    symbol: str
    entry_price: float
    entry_candle_low: float
    qty_initial: int
    qty_open: int
    high_water: float
    entry_time: datetime
    use_credit: bool = False
    tp1_done: bool = False
    tp2_done: bool = False
    time_stop_done: bool = False    # 메인 타임스톱 청산 1회 실행 여부(재발 방지)
    time_stop_first_done: bool = False  # 1차 타임스톱 체크 1회 실행 여부
    # §5.3 ATR로 사전 지정된 익절 목표가. None이면 ExitParams 폴백(범위 하단) 사용.
    tp1_target: float | None = None
    tp2_target: float | None = None


@dataclass(frozen=True)
class ExitAction:
    kind: ExitKind
    ratio: float        # 현재 qty_open 대비 청산 비율 (0.0 = HOLD)
    reason: str

    @property
    def counter(self) -> CounterEffect:
        return _counter_effect(self.kind)

    @property
    def is_exit(self) -> bool:
        return self.kind != ExitKind.HOLD


_HOLD = ExitAction(ExitKind.HOLD, 0.0, "hold")


# ─────────────────────────── 친화 사유 변환 (요구 1) ───────────────────────────


def _portion_word(ratio: float) -> str:
    """청산 비율을 쉬운 말로: 전량/절반/40% 등."""
    if ratio >= 0.999:
        return "전량"
    if abs(ratio - 0.5) < 0.01:
        return "절반"
    return f"{round(ratio * 100)}%"


def friendly_exit_reason(
    kind: ExitKind, pnl_pct: float, *, ratio: float, time_stop_minutes: int = 30,
) -> str:
    """청산 사유를 누구나 이해할 쉬운 한국어 + 손익률로 변환(요구 1).

    예) TECHNICAL → "📉 진입할 때 저점 밑으로 떨어져서 손절했어요 (-1.11%)"
        TIME_STOP → "⏱ 30분 기다렸는데 방향이 안 나와서 절반 팔았어요 (-0.47%)"
    """
    pnl = f"({pnl_pct:+.2%})"
    portion = _portion_word(ratio)
    if kind == ExitKind.EOD_FORCE:
        return f"🔔 장 마감이라 보유분 {portion} 정리했어요 {pnl}"
    if kind == ExitKind.HARD_STOP:
        return f"🛑 손실이 더 커지기 전에 {portion} 손절했어요 {pnl}"
    if kind == ExitKind.TECHNICAL:
        return f"📉 진입할 때 저점 밑으로 떨어져서 손절했어요 {pnl}"
    if kind == ExitKind.SIGNAL_BREAKDOWN:
        return f"⚠️ 진입 근거가 무너져서 {portion} 정리했어요 {pnl}"
    if kind == ExitKind.TRAILING:
        return f"📈 고점에서 밀려서 수익 지키며 {portion} 정리했어요 {pnl}"
    if kind == ExitKind.TP1:
        return f"✅ 1차 목표가 도달! {portion} 익절했어요 {pnl}"
    if kind == ExitKind.TP2:
        return f"✅ 2차 목표가 도달! {portion} 더 익절했어요 {pnl}"
    if kind == ExitKind.TIME_STOP:
        return f"⏱ {time_stop_minutes}분 기다렸는데 방향이 안 나와서 {portion} 팔았어요 {pnl}"
    if kind == ExitKind.TIME_STOP_FIRST:
        return f"⏱ {time_stop_minutes}분 1차 점검 — 방향이 안 나와서 {portion} 정리했어요 {pnl}"
    if kind == ExitKind.HOLD:
        return "보유 중이에요"
    return f"{kind.value} {pnl}"


# ─────────────────────────── evaluator ───────────────────────────


def evaluate_exit(
    state: LivePositionState,
    *,
    price: float,
    now: datetime,
    breakdown_count: int,
    params: ExitParams,
) -> ExitAction:
    """현재가/시각/신호붕괴 카운트로 청산 판정.

    우선순위(먼저 도달한 쪽, §5.2 매도 타점):
      EOD 강제청산(15:20) → 하드 -3% → 기술적(진입캔들 저점 이탈) → 트레일링(TP1 후,
      고점 -1.5%) → TP1(+3~5%, 50%) → TP2(+6~8%, 30%) → 타임스톱(30분, 50%) → HOLD

    **신호붕괴(거래량 소멸/MACD 데드 등)로 파는 '조급한·신호 없는 청산' 로직은
    제거되었다** — 손절은 오직 진입캔들 저점 이탈(기술적)·하드 -3%로만, 익절은 ATR
    목표가/트레일링으로만 발동한다(``breakdown_count``는 더 이상 청산을 유발하지 않음).

    **EOD 강제청산(§5.7)**: 당일 매수분은 **어떤 조건에서도 예외 없이** 장 마감 전
    15:20에 전량 시장가 청산한다. 보유 종목을 다음 거래일로 이월하지 않으며, 이 규칙은
    백테스트·실거래 동일하게 적용된다(우회 플래그 없음).
    """
    if state.entry_price <= 0 or state.qty_open <= 0:
        return _HOLD

    local = now.astimezone(KST).time()
    ret = (price - state.entry_price) / state.entry_price
    elapsed_min = (now - state.entry_time).total_seconds() / 60.0

    # §5.3 ATR 기반 익절 목표가 (사전 지정). 미지정 시 ExitParams 폴백.
    tp1_target = state.tp1_target if state.tp1_target is not None else params.tp1_pct
    tp2_target = state.tp2_target if state.tp2_target is not None else params.tp2_pct

    # 0) EOD 강제청산 (15:20) — 최우선·무조건, 보유분 전량 (§5.7).
    #    당일 매수분은 어떤 조건에서도(손익/신호 무관) 예외 없이 장 마감 전 전량 청산되어
    #    다음 거래일로 이월되지 않는다. 우회 플래그를 두지 않는다.
    if local >= params.force_close_time:
        return ExitAction(
            ExitKind.EOD_FORCE, 1.0,
            f"EOD 강제청산 (now={local.isoformat()} ≥ {params.force_close_time.isoformat()})",
        )

    # 1) 하드 손절 (-3%)
    if ret <= params.hard_stop_pct:
        return ExitAction(
            ExitKind.HARD_STOP, 1.0,
            f"하드 손절 {ret:+.2%} ≤ {params.hard_stop_pct:+.2%}",
        )

    # 2) 기술적 손절: 진입 캔들 저점 -0.5% 이탈 (1분봉 종가 기준, §5.4 개정 — 너무 빡빡하지 않게)
    if params.use_technical_stop and state.entry_candle_low > 0:
        trigger = state.entry_candle_low * (1 - params.technical_buffer_pct)
        if price < trigger:
            return ExitAction(
                ExitKind.TECHNICAL, 1.0,
                f"진입캔들 저점 -{params.technical_buffer_pct:.1%} 이탈 "
                f"price={price:.0f} < {trigger:.0f}(저점 {state.entry_candle_low:.0f})",
            )

    # (신호붕괴 청산 제거됨 — '신호 없이 파는' 조급한 청산 금지. breakdown_count 무시.)
    del breakdown_count

    # 4) 트레일링 (1차 익절 이후에만 활성)
    #    트레일링 종료 컷오프(15:20)는 위 EOD 강제청산이 무조건 처리하므로 별도 분기 불필요.
    if state.tp1_done and state.high_water > 0:
        drop = (state.high_water - price) / state.high_water
        if drop >= params.trailing_pct:
            return ExitAction(
                ExitKind.TRAILING, 1.0,
                f"트레일링 고점대비 {drop:.2%} ≥ {params.trailing_pct:.2%}",
            )

    # 5) 익절 1차 (ATR 동적 +3~5%): 보유분의 tp1_close_ratio 청산
    if not state.tp1_done and ret >= tp1_target:
        return ExitAction(
            ExitKind.TP1, params.tp1_close_ratio,
            f"1차 익절 {ret:+.2%} ≥ {tp1_target:+.2%} ({params.tp1_close_ratio:.0%})",
        )

    # 6) 익절 2차 (ATR 동적 +6~8%): 잔여분의 tp2_close_ratio 청산
    if state.tp1_done and not state.tp2_done and ret >= tp2_target:
        return ExitAction(
            ExitKind.TP2, params.tp2_close_ratio,
            f"2차 익절 {ret:+.2%} ≥ {tp2_target:+.2%} ({params.tp2_close_ratio:.0%})",
        )

    # (이하 친화 사유 변환은 friendly_exit_reason 참고)
    # 7) 타임스톱 (§5.5 개정, 2단 체크) — enabled일 때만, 메인 미실행 상태에서.
    #    1차 체크(first_check_minutes)는 메인 체크 전 구간에서 1회 평가하고, 메인 체크
    #    (evaluation_minutes)는 경과+수익 미달 시 action(reduce_50/exit_all)으로 청산한다.
    #    모든 타임스톱 청산은 중립(연속손절 카운터 미산입, §5.5).
    if params.time_stop_enabled and not state.time_stop_done:
        # 7a) 1차 체크: [first, main) 구간에서 1회. hold면 비청산(보류).
        if (
            params.time_stop_first_minutes > 0
            and not state.time_stop_first_done
            and params.time_stop_first_minutes <= elapsed_min < params.time_stop_minutes
            and ret < params.time_stop_first_min_profit
        ):
            ratio = action_ratio(params.time_stop_first_action)
            if ratio > 0:
                return ExitAction(
                    ExitKind.TIME_STOP_FIRST, ratio,
                    f"1차 타임스톱 {elapsed_min:.0f}분 경과 + 수익 미달({ret:+.2%} < "
                    f"{params.time_stop_first_min_profit:+.2%}) → {params.time_stop_first_action}",
                )
        # 7b) 메인 체크
        if (
            elapsed_min >= params.time_stop_minutes
            and ret < params.time_stop_min_profit
        ):
            ratio = action_ratio(params.time_stop_action)
            if ratio > 0:
                return ExitAction(
                    ExitKind.TIME_STOP, ratio,
                    f"타임스톱 {elapsed_min:.0f}분 경과 + 수익 미달({ret:+.2%} < "
                    f"{params.time_stop_min_profit:+.2%}) → {params.time_stop_action}",
                )

    return _HOLD
