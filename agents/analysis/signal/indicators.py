"""일봉 + 분봉 복합 진입 분석 (CLAUDE.md §2.3 / §5.2 개정).

분봉만 보고 진입해 큰 추세를 놓치던 문제를 해결하기 위해 **일봉 추세 게이트 + 5분봉
돌파 타점**의 복합 분석으로 교체한다. 일봉 게이트가 통과해야만 분봉 타점을 본다.

매수 조건(요구):
  1. 일봉 게이트(전부 충족, 필수):
     - 일봉 종가 > 일봉 20MA (정배열 위)
     - 일봉 RSI ≥ 50
     - 최근 3일 중 양봉 ≥ 2개
     - 전일 거래량 ≥ 5일 평균 × 150%
  2. 분봉 진입 타점(5분봉):
     - 직전 고점 돌파
     - 돌파 캔들 거래량 ≥ 직전 캔들 × 200%
     - RSI 50~65
     - MACD 히스토그램 양전환
  3. 진입 금지(하나라도 해당 시 NO_ENTRY):
     - 일봉 저항선 근처(고점 대비 -2% 이내)
     - 일봉 RSI ≥ 70(과매수)
     - 3일 연속 음봉

판정: 일봉 게이트 통과 + 분봉 타점 4개 → ``STRONG_ENTRY`` / 3개 → ``CONDITIONAL_ENTRY``.
음봉 추격(분봉 마지막이 음봉)·진입 금지·일봉 게이트 실패는 무조건 ``NO_ENTRY``.
일봉 데이터가 부족하면(백테스트 초기 등) 일봉 게이트는 '미확인'으로 보고 분봉 타점만으로
판정하되(시스템 무거래 자가정지 방지, §19), ``daily_strong=False``로 사이즈를 보수화한다.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import yaml

from core.indicators import (
    CandleLike,
    atr_pct_from_candles,
    macd as macd_calc,
    rsi as rsi_calc,
    sma,
)

KST = timezone(timedelta(hours=9))


# ─────────────────────────── enums ───────────────────────────


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class Signal(str, Enum):
    STRONG_ENTRY = "STRONG_ENTRY"
    CONDITIONAL_ENTRY = "CONDITIONAL_ENTRY"
    NO_ENTRY = "NO_ENTRY"


# ─────────────────────────── config ───────────────────────────


@dataclass(frozen=True)
class SignalParams:
    volume_surge_multiplier: float
    rsi_period: int
    rsi_oversold: float
    rsi_overbought: float
    macd_fast: int
    macd_slow: int
    macd_signal: int
    ma_periods: tuple[int, int, int]      # (5, 20, 60)
    strong_min_indicators: int             # 4 (분봉 타점 4개)
    conditional_min_indicators: int        # 3
    candle_long: tuple[str, ...]
    candle_short: tuple[str, ...]
    atr_period: int = 14
    # 분봉 진입 타점 (§5.2): RSI 50~65, 직전 고점 돌파, 돌파 거래량 200%.
    rsi_entry_low: float = 50.0
    rsi_entry_high: float = 65.0
    breakout_lookback: int = 10            # 직전 고점 산출 윈도우(직전 N봉)
    breakout_volume_mult: float = 2.0      # 돌파 캔들 거래량 ≥ 직전 캔들 × 200%
    # 일봉 게이트 (§5.2 개정).
    daily_ma_period: int = 20
    daily_rsi_period: int = 14
    daily_rsi_min: float = 50.0
    daily_rsi_overbought: float = 70.0
    daily_recent_days: int = 3
    daily_bullish_min: int = 2
    daily_vol_lookback: int = 5
    daily_vol_mult: float = 1.5
    # 진입 금지: 저항선 근접(고점 대비 -2% 이내), 3일 연속 음봉.
    resistance_pct: float = 0.02
    resistance_lookback: int = 20
    consecutive_down: int = 3

    @classmethod
    def from_file(cls, path: Path) -> "SignalParams":
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        s = doc["signal"]
        periods = tuple(s["ma_periods"])
        if len(periods) != 3:
            raise ValueError(f"ma_periods must have exactly 3 entries, got {periods}")
        rsi = s["rsi"]
        entry = rsi.get("entry_zone", [50, 65])
        brk = s.get("breakout", {})
        daily = s.get("daily", {})
        block = s.get("entry_block", {})
        return cls(
            volume_surge_multiplier=float(s["volume_surge_multiplier"]),
            rsi_period=int(rsi["period"]),
            rsi_oversold=float(rsi["oversold"]),
            rsi_overbought=float(rsi["overbought"]),
            macd_fast=int(s["macd"]["fast"]),
            macd_slow=int(s["macd"]["slow"]),
            macd_signal=int(s["macd"]["signal"]),
            ma_periods=periods,  # type: ignore[arg-type]
            strong_min_indicators=int(s["entry_rules"]["strong_min_indicators"]),
            conditional_min_indicators=int(s["entry_rules"]["conditional_min_indicators"]),
            candle_long=tuple(s["candle_patterns"]["long"]),
            candle_short=tuple(s["candle_patterns"]["short"]),
            atr_period=int(s.get("atr_period", 14)),
            rsi_entry_low=float(entry[0]),
            rsi_entry_high=float(entry[-1]),
            breakout_lookback=int(brk.get("lookback", 10)),
            breakout_volume_mult=float(brk.get("volume_mult", 2.0)),
            daily_ma_period=int(daily.get("ma_period", 20)),
            daily_rsi_period=int(daily.get("rsi_period", 14)),
            daily_rsi_min=float(daily.get("rsi_min", 50.0)),
            daily_rsi_overbought=float(daily.get("rsi_overbought", 70.0)),
            daily_recent_days=int(daily.get("recent_days", 3)),
            daily_bullish_min=int(daily.get("bullish_min", 2)),
            daily_vol_lookback=int(daily.get("vol_lookback", 5)),
            daily_vol_mult=float(daily.get("vol_mult", 1.5)),
            resistance_pct=float(block.get("resistance_pct", 0.02)),
            resistance_lookback=int(block.get("resistance_lookback", 20)),
            consecutive_down=int(block.get("consecutive_down", 3)),
        )


# ─────────────────────────── decision ───────────────────────────


@dataclass(frozen=True)
class IndicatorScore:
    name: str
    passed: bool
    detail: str
    value: float | None = None


@dataclass(frozen=True)
class DailyCheck:
    """일봉 게이트 + 진입금지 평가 결과."""

    sufficient: bool                       # 일봉 데이터 충분 여부
    gate_passed: bool                      # 4개 일봉 조건 전부 충족
    blocked: bool                          # 진입금지 3개 중 하나라도 해당
    checks: tuple[IndicatorScore, ...]     # 세부(게이트 + 금지)
    block_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class SignalDecision:
    symbol: str
    timestamp: datetime
    direction: Direction
    signal: Signal
    score_count: int                       # 분봉 타점 통과 수(0~4)
    scores: tuple[IndicatorScore, ...]
    entry_candle_low: int
    entry_candle_high: int
    reason_text: str
    atr_pct: float | None = None
    daily_strong: bool = False             # 일봉 게이트 통과(강세) → 사이즈 ↑ (§5 사이징)
    daily: DailyCheck | None = None


# ─────────────────────────── analyzer ───────────────────────────


class SignalAnalyzer:
    """일봉 게이트 + 5분봉 돌파 타점 복합 평가기."""

    def __init__(self, params: SignalParams) -> None:
        self._p = params

    @property
    def params(self) -> SignalParams:
        return self._p

    def evaluate(
        self,
        symbol: str,
        candles: Sequence[CandleLike],
        *,
        direction: Direction,
        now: datetime | None = None,
        daily_candles: Sequence[CandleLike] | None = None,
    ) -> SignalDecision:
        if not candles:
            raise ValueError("candles must not be empty")

        # 분봉 진입 타점 4개 (§5.2).
        minute_scores = (
            self._eval_breakout(candles, direction),
            self._eval_volume(candles, direction),
            self._eval_rsi(candles, direction),
            self._eval_macd(candles, direction),
        )
        minute_count = sum(1 for s in minute_scores if s.passed)

        last = candles[-1]
        # 음봉 추격 금지(하드 게이트): LONG은 양봉, SHORT는 음봉이어야 한다.
        bar_ok = last.c > last.o if direction == Direction.LONG else last.c < last.o
        candle_gate = IndicatorScore(
            "candle", bar_ok,
            ("양봉" if bar_ok else "음봉") + f" (o={last.o:.0f}, c={last.c:.0f})",
        )

        # 일봉 게이트 + 진입금지 (§5.2 개정).
        daily = self._eval_daily(daily_candles, price=float(last.c), direction=direction)

        gated = (
            (not bar_ok)
            or daily.blocked
            or (daily.sufficient and not daily.gate_passed)
        )
        signal = self._classify(minute_count, gated=gated)
        daily_strong = daily.sufficient and daily.gate_passed

        scores = minute_scores + (candle_gate,) + daily.checks
        return SignalDecision(
            symbol=symbol,
            timestamp=now or datetime.now(KST),
            direction=direction,
            signal=signal,
            score_count=minute_count,
            scores=scores,
            entry_candle_low=int(last.l),
            entry_candle_high=int(last.h),
            reason_text=self._compose_reason(
                minute_scores, minute_count, signal, direction, bar_ok, daily,
                daily_strong,
            ),
            atr_pct=atr_pct_from_candles(candles, period=self._p.atr_period),
            daily_strong=daily_strong,
            daily=daily,
        )

    def _classify(self, count: int, *, gated: bool = False) -> Signal:
        if gated:
            return Signal.NO_ENTRY
        if count >= self._p.strong_min_indicators:
            return Signal.STRONG_ENTRY
        if count >= self._p.conditional_min_indicators:
            return Signal.CONDITIONAL_ENTRY
        return Signal.NO_ENTRY

    # ─── 친화 사유 (요구 1) ───

    @staticmethod
    def _compose_reason(
        minute_scores: Sequence[IndicatorScore],
        count: int,
        signal: Signal,
        direction: Direction,
        bar_ok: bool,
        daily: "DailyCheck",
        daily_strong: bool,
    ) -> str:
        _ko = {"breakout": "고점돌파", "volume": "거래량폭발", "rsi": "RSI구간", "macd": "MACD양전환"}
        passed = [_ko.get(s.name, s.name) for s in minute_scores if s.passed]
        passed_txt = "·".join(passed) if passed else "없음"

        if signal == Signal.NO_ENTRY:
            if not bar_ok:
                return "⛔ 진입 안 함 — 마지막 분봉이 음봉이라 추격 매수 금지"
            if daily.blocked:
                return "⛔ 진입 안 함 — " + (daily.block_reasons[0] if daily.block_reasons
                                          else "일봉 진입금지 구간")
            if daily.sufficient and not daily.gate_passed:
                fails = [c.detail for c in daily.checks if c.name.startswith("daily_") and not c.passed]
                why = fails[0] if fails else "일봉 추세 약함"
                return f"⛔ 진입 안 함 — 일봉 게이트 미충족({why})"
            return f"⛔ 진입 안 함 — 분봉 타점 {count}/4만 충족({passed_txt})"

        if signal == Signal.STRONG_ENTRY:
            head = "🚀 일봉 추세 양호 + 분봉 타점 4/4" if daily_strong \
                else "🚀 분봉 타점 4/4(일봉 미확인)"
            return f"{head} 충족 → 강하게 진입 ({passed_txt})"
        # CONDITIONAL
        head = "👍 일봉 양호 + 분봉 타점 3/4" if daily_strong else "👍 분봉 타점 3/4"
        return f"{head} 충족 → 조건부 진입 ({passed_txt})"

    # ─── 분봉 타점 (4개, §5.2) ───

    def _eval_breakout(
        self, candles: Sequence[CandleLike], direction: Direction,
    ) -> IndicatorScore:
        """직전 N봉 고점 돌파(LONG) / 저점 이탈(SHORT)."""
        n = self._p.breakout_lookback
        if len(candles) < n + 1:
            return IndicatorScore("breakout", False, "데이터 부족")
        last = candles[-1]
        prior = candles[-(n + 1):-1]
        if direction == Direction.LONG:
            prior_high = max(float(c.h) for c in prior)
            passed = float(last.c) > prior_high
            detail = f"종가 {last.c:.0f} {'>' if passed else '≤'} 직전{n}봉 고점 {prior_high:.0f}"
            val = float(last.c) - prior_high
        else:
            prior_low = min(float(c.l) for c in prior)
            passed = float(last.c) < prior_low
            detail = f"종가 {last.c:.0f} {'<' if passed else '≥'} 직전{n}봉 저점 {prior_low:.0f}"
            val = float(last.c) - prior_low
        return IndicatorScore("breakout", passed, detail, val)

    def _eval_volume(
        self, candles: Sequence[CandleLike], direction: Direction,
    ) -> IndicatorScore:
        """돌파 캔들 거래량 ≥ 직전 캔들 × breakout_volume_mult(200%)."""
        if len(candles) < 2:
            return IndicatorScore("volume", False, "데이터 부족")
        last, prev = candles[-1], candles[-2]
        v_now = float(last.v)        # type: ignore[attr-defined]
        v_prev = float(prev.v)       # type: ignore[attr-defined]
        mult = self._p.breakout_volume_mult
        ratio = (v_now / v_prev) if v_prev > 0 else 0.0
        is_dir_bar = last.c > last.o if direction == Direction.LONG else last.c < last.o
        passed = v_prev > 0 and ratio >= mult and is_dir_bar
        return IndicatorScore(
            "volume", passed,
            f"직전 캔들 대비 {ratio:.2f}x (≥{mult:.1f}x)", ratio,
        )

    def _eval_rsi(
        self, candles: Sequence[CandleLike], direction: Direction,
    ) -> IndicatorScore:
        """RSI 50~65 구간(LONG). SHORT는 대칭(35~50)."""
        closes = [float(c.c) for c in candles]
        if len(closes) < self._p.rsi_period + 2:
            return IndicatorScore("rsi", False, "데이터 부족")
        vals = rsi_calc(closes, period=self._p.rsi_period)
        cur = vals[-1]
        if cur is None:
            return IndicatorScore("rsi", False, "지표 부족")
        if direction == Direction.LONG:
            lo, hi = self._p.rsi_entry_low, self._p.rsi_entry_high
        else:
            lo, hi = 100.0 - self._p.rsi_entry_high, 100.0 - self._p.rsi_entry_low
        passed = lo <= cur <= hi
        return IndicatorScore("rsi", passed, f"rsi={cur:.1f} (구간 {lo:.0f}~{hi:.0f})", cur)

    def _eval_macd(
        self, candles: Sequence[CandleLike], direction: Direction,
    ) -> IndicatorScore:
        """MACD 히스토그램 양전환 또는 양(+) 확대(LONG)."""
        closes = [float(c.c) for c in candles]
        warmup = self._p.macd_slow + self._p.macd_signal
        if len(closes) < warmup:
            return IndicatorScore("macd", False, "데이터 부족")
        _m, _s, hist = macd_calc(
            closes, fast=self._p.macd_fast, slow=self._p.macd_slow,
            signal=self._p.macd_signal,
        )
        h_now, h_prev = hist[-1], hist[-2]
        if h_now is None or h_prev is None:
            return IndicatorScore("macd", False, "지표 부족")
        if direction == Direction.LONG:
            turned = h_prev <= 0 < h_now
            expanding = h_now > 0 and h_now > h_prev
            passed = turned or expanding
        else:
            turned = h_prev >= 0 > h_now
            expanding = h_now < 0 and h_now < h_prev
            passed = turned or expanding
        return IndicatorScore("macd", passed, f"hist {h_prev:+.4f}→{h_now:+.4f}", h_now)

    # ─── 일봉 게이트 + 진입금지 (§5.2 개정) ───

    def _eval_daily(
        self,
        daily: Sequence[CandleLike] | None,
        *,
        price: float,
        direction: Direction,
    ) -> DailyCheck:
        p = self._p
        need = max(p.daily_ma_period, p.daily_rsi_period + 1,
                   p.daily_vol_lookback + 1, p.daily_recent_days)
        if direction != Direction.LONG or daily is None or len(daily) < need:
            # SHORT 미지원 또는 일봉 데이터 부족 → 게이트 '미확인'(분봉만으로 판정, §19).
            note = "SHORT" if direction != Direction.LONG else "일봉 데이터 부족(미확인)"
            return DailyCheck(
                sufficient=False, gate_passed=False, blocked=False,
                checks=(IndicatorScore("daily", False, note),),
            )

        closes = [float(c.c) for c in daily]
        opens = [float(c.o) for c in daily]
        highs = [float(c.h) for c in daily]
        vols = [float(getattr(c, "v")) for c in daily]

        # 1) 일봉 종가 > 20MA
        ma = sma(closes, p.daily_ma_period)[-1]
        c_ma = IndicatorScore(
            "daily_ma", ma is not None and closes[-1] > ma,
            f"일봉종가 {closes[-1]:.0f} {'>' if (ma is not None and closes[-1] > ma) else '≤'} "
            f"{p.daily_ma_period}MA {ma:.0f}" if ma is not None else f"{p.daily_ma_period}MA 미산출",
            value=ma,
        )
        # 2) 일봉 RSI ≥ 50
        drsi = rsi_calc(closes, period=p.daily_rsi_period)[-1]
        c_rsi = IndicatorScore(
            "daily_rsi", drsi is not None and drsi >= p.daily_rsi_min,
            f"일봉 RSI {drsi:.1f} (≥{p.daily_rsi_min:.0f})" if drsi is not None else "RSI 미산출",
            value=drsi,
        )
        # 3) 최근 3일 중 양봉 ≥ 2
        recent = list(zip(opens, closes))[-p.daily_recent_days:]
        bull = sum(1 for o, c in recent if c > o)
        c_bull = IndicatorScore(
            "daily_bullish", bull >= p.daily_bullish_min,
            f"최근 {p.daily_recent_days}일 양봉 {bull}개 (≥{p.daily_bullish_min})",
            value=float(bull),
        )
        # 4) 전일 거래량 ≥ 5일 평균 × 150%
        prev_vol = vols[-1]
        base = vols[-(p.daily_vol_lookback + 1):-1]
        avg_vol = (sum(base) / len(base)) if base else 0.0
        vol_ok = avg_vol > 0 and prev_vol >= avg_vol * p.daily_vol_mult
        c_vol = IndicatorScore(
            "daily_volume", vol_ok,
            f"전일거래량 {prev_vol:.0f} ≥ {p.daily_vol_lookback}일평균 {avg_vol:.0f}×{p.daily_vol_mult:.1f}",
            value=(prev_vol / avg_vol) if avg_vol > 0 else None,
        )

        gate_passed = c_ma.passed and c_rsi.passed and c_bull.passed and c_vol.passed

        # 진입 금지 3개.
        block_reasons: list[str] = []
        # (a) 저항선 근접: 최근 N일 고점 대비 -resistance_pct 이내
        res_high = max(highs[-p.resistance_lookback:])
        near_res = price >= res_high * (1 - p.resistance_pct)
        c_res = IndicatorScore(
            "block_resistance", not near_res,
            f"현재가 {price:.0f} vs 저항 {res_high:.0f} (-{p.resistance_pct:.0%} 이내={near_res})",
        )
        if near_res:
            block_reasons.append(f"일봉 저항선 근처(고점 {res_high:.0f} 대비 -{p.resistance_pct:.0%} 이내)")
        # (b) 일봉 RSI 과매수
        overbought = drsi is not None and drsi >= p.daily_rsi_overbought
        c_ob = IndicatorScore(
            "block_overbought", not overbought,
            f"일봉 RSI {drsi:.1f} (과매수 {p.daily_rsi_overbought:.0f}↑={overbought})"
            if drsi is not None else "RSI 미산출",
        )
        if overbought:
            block_reasons.append(f"일봉 RSI 과매수({drsi:.0f} ≥ {p.daily_rsi_overbought:.0f})")
        # (c) 3일 연속 음봉
        tail = list(zip(opens, closes))[-p.consecutive_down:]
        all_down = len(tail) >= p.consecutive_down and all(c < o for o, c in tail)
        c_down = IndicatorScore(
            "block_consec_down", not all_down,
            f"{p.consecutive_down}일 연속 음봉={all_down}",
        )
        if all_down:
            block_reasons.append(f"{p.consecutive_down}일 연속 음봉")

        blocked = bool(block_reasons)
        return DailyCheck(
            sufficient=True,
            gate_passed=gate_passed and not blocked,
            blocked=blocked,
            checks=(c_ma, c_rsi, c_bull, c_vol, c_res, c_ob, c_down),
            block_reasons=tuple(block_reasons),
        )
