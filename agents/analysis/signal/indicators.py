"""5개 지표 종합 평가 및 진입 강도 판정 (CLAUDE.md §2.3 / §5.2).

거래량 / RSI / MACD / 이동평균 / 캔들 패턴 5개를 종합하여
``STRONG_ENTRY`` (4+ 충족) / ``CONDITIONAL_ENTRY`` (3 충족) / ``NO_ENTRY``
판정을 생성한다.
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
    is_bearish_engulfing,
    is_bullish_engulfing,
    is_hammer,
    is_long_bearish,
    is_long_bullish,
    is_shooting_star,
    macd as macd_calc,
    rsi as rsi_calc,
    sma,
    volume_spike_ratio,
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
    strong_min_indicators: int             # 4
    conditional_min_indicators: int        # 3
    candle_long: tuple[str, ...]
    candle_short: tuple[str, ...]

    @classmethod
    def from_file(cls, path: Path) -> "SignalParams":
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        s = doc["signal"]
        periods = tuple(s["ma_periods"])
        if len(periods) != 3:
            raise ValueError(f"ma_periods must have exactly 3 entries, got {periods}")
        return cls(
            volume_surge_multiplier=float(s["volume_surge_multiplier"]),
            rsi_period=int(s["rsi"]["period"]),
            rsi_oversold=float(s["rsi"]["oversold"]),
            rsi_overbought=float(s["rsi"]["overbought"]),
            macd_fast=int(s["macd"]["fast"]),
            macd_slow=int(s["macd"]["slow"]),
            macd_signal=int(s["macd"]["signal"]),
            ma_periods=periods,  # type: ignore[arg-type]
            strong_min_indicators=int(s["entry_rules"]["strong_min_indicators"]),
            conditional_min_indicators=int(s["entry_rules"]["conditional_min_indicators"]),
            candle_long=tuple(s["candle_patterns"]["long"]),
            candle_short=tuple(s["candle_patterns"]["short"]),
        )


# ─────────────────────────── decision ───────────────────────────


@dataclass(frozen=True)
class IndicatorScore:
    name: str           # "volume" | "rsi" | "macd" | "ma" | "candle"
    passed: bool
    detail: str
    value: float | None = None


@dataclass(frozen=True)
class SignalDecision:
    symbol: str
    timestamp: datetime
    direction: Direction
    signal: Signal
    score_count: int
    scores: tuple[IndicatorScore, ...]
    entry_candle_low: int
    entry_candle_high: int
    reason_text: str


# ─────────────────────────── analyzer ───────────────────────────


class SignalAnalyzer:
    """5개 지표 종합 평가기. 호출자는 분봉 시퀀스를 넘긴다."""

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
    ) -> SignalDecision:
        if not candles:
            raise ValueError("candles must not be empty")
        scores = (
            self._eval_volume(candles, direction),
            self._eval_rsi(candles, direction),
            self._eval_macd(candles, direction),
            self._eval_ma(candles, direction),
            self._eval_candle(candles, direction),
        )
        count = sum(1 for s in scores if s.passed)
        signal = self._classify(count)
        last = candles[-1]
        return SignalDecision(
            symbol=symbol,
            timestamp=now or datetime.now(KST),
            direction=direction,
            signal=signal,
            score_count=count,
            scores=scores,
            entry_candle_low=int(last.l),
            entry_candle_high=int(last.h),
            reason_text=self._compose_reason(scores, count, signal, direction),
        )

    def _classify(self, count: int) -> Signal:
        if count >= self._p.strong_min_indicators:
            return Signal.STRONG_ENTRY
        if count >= self._p.conditional_min_indicators:
            return Signal.CONDITIONAL_ENTRY
        return Signal.NO_ENTRY

    @staticmethod
    def _compose_reason(
        scores: Sequence[IndicatorScore],
        count: int,
        signal: Signal,
        direction: Direction,
    ) -> str:
        passed = [s.name for s in scores if s.passed]
        return (
            f"{direction.value.upper()} {signal.value} "
            f"({count}/5: {', '.join(passed) or 'none'})"
        )

    # ─── 지표별 평가 ───

    def _eval_volume(
        self, candles: Sequence[CandleLike], direction: Direction,
    ) -> IndicatorScore:
        volumes = [int(c.v) for c in candles]  # type: ignore[attr-defined]
        if len(volumes) < 21:
            return IndicatorScore("volume", False, "데이터 부족 (<21 캔들)")
        ratio = volume_spike_ratio(volumes, window=20)
        last = candles[-1]
        is_up_bar = last.c > last.o
        is_down_bar = last.c < last.o
        threshold = self._p.volume_surge_multiplier
        if direction == Direction.LONG:
            passed = ratio is not None and ratio >= threshold and is_up_bar
        else:
            passed = ratio is not None and ratio >= threshold and is_down_bar
        ratio_s = f"{ratio:.2f}x" if ratio is not None else "n/a"
        return IndicatorScore(
            "volume",
            passed,
            f"ratio={ratio_s} (≥{threshold:.1f}x), up_bar={is_up_bar}",
            ratio,
        )

    def _eval_rsi(
        self, candles: Sequence[CandleLike], direction: Direction,
    ) -> IndicatorScore:
        closes = [float(c.c) for c in candles]
        if len(closes) < self._p.rsi_period + 2:
            return IndicatorScore("rsi", False, "데이터 부족")
        vals = rsi_calc(closes, period=self._p.rsi_period)
        cur, prev = vals[-1], vals[-2]
        if cur is None or prev is None:
            return IndicatorScore("rsi", False, "지표 부족")
        in_range = self._p.rsi_oversold < cur < self._p.rsi_overbought
        if direction == Direction.LONG:
            passed = in_range and cur > prev
        else:
            passed = in_range and cur < prev
        return IndicatorScore(
            "rsi", passed, f"rsi={cur:.1f}, prev={prev:.1f}, in_range={in_range}", cur,
        )

    def _eval_macd(
        self, candles: Sequence[CandleLike], direction: Direction,
    ) -> IndicatorScore:
        closes = [float(c.c) for c in candles]
        warmup = self._p.macd_slow + self._p.macd_signal
        if len(closes) < warmup:
            return IndicatorScore("macd", False, "데이터 부족")
        macd_line, sig_line, hist = macd_calc(
            closes,
            fast=self._p.macd_fast,
            slow=self._p.macd_slow,
            signal=self._p.macd_signal,
        )
        if any(v is None for v in (macd_line[-1], macd_line[-2], sig_line[-1], sig_line[-2], hist[-1], hist[-2])):
            return IndicatorScore("macd", False, "지표 부족")
        m_now, m_prev = macd_line[-1], macd_line[-2]
        s_now, s_prev = sig_line[-1], sig_line[-2]
        h_now, h_prev = hist[-1], hist[-2]
        assert m_now is not None and m_prev is not None
        assert s_now is not None and s_prev is not None
        assert h_now is not None and h_prev is not None
        if direction == Direction.LONG:
            cross = m_now > s_now and m_prev <= s_prev
        else:
            cross = m_now < s_now and m_prev >= s_prev
        expanding = abs(h_now) > abs(h_prev)
        passed = cross and expanding
        return IndicatorScore(
            "macd",
            passed,
            f"cross={cross}, hist_expand={expanding}, hist={h_now:.4f}",
            h_now,
        )

    def _eval_ma(
        self, candles: Sequence[CandleLike], direction: Direction,
    ) -> IndicatorScore:
        closes = [float(c.c) for c in candles]
        short_p, mid_p, long_p = self._p.ma_periods
        if len(closes) < long_p:
            return IndicatorScore("ma", False, "데이터 부족")
        ma_short = sma(closes, short_p)[-1]
        ma_mid = sma(closes, mid_p)[-1]
        ma_long = sma(closes, long_p)[-1]
        if ma_short is None or ma_mid is None or ma_long is None:
            return IndicatorScore("ma", False, "지표 부족")
        if direction == Direction.LONG:
            aligned = ma_short > ma_mid > ma_long
        else:
            aligned = ma_short < ma_mid < ma_long
        return IndicatorScore(
            "ma",
            aligned,
            f"{short_p}MA={ma_short:.0f}, {mid_p}MA={ma_mid:.0f}, {long_p}MA={ma_long:.0f}",
            ma_short - ma_long,
        )

    def _eval_candle(
        self, candles: Sequence[CandleLike], direction: Direction,
    ) -> IndicatorScore:
        if len(candles) < 2:
            return IndicatorScore("candle", False, "데이터 부족")
        cur, prev = candles[-1], candles[-2]
        matched: list[str] = []
        if direction == Direction.LONG:
            patterns = self._p.candle_long
            if "hammer" in patterns and is_hammer(cur):
                matched.append("hammer")
            if "long_bullish" in patterns and is_long_bullish(cur):
                matched.append("long_bullish")
            if "bullish_engulfing" in patterns and is_bullish_engulfing(prev, cur):
                matched.append("bullish_engulfing")
        else:
            patterns = self._p.candle_short
            if "shooting_star" in patterns and is_shooting_star(cur):
                matched.append("shooting_star")
            if "long_bearish" in patterns and is_long_bearish(cur):
                matched.append("long_bearish")
            if "bearish_engulfing" in patterns and is_bearish_engulfing(prev, cur):
                matched.append("bearish_engulfing")
        return IndicatorScore(
            "candle", bool(matched), f"matched={matched or 'none'}",
        )
