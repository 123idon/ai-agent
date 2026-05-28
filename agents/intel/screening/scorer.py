"""Screening score calculator (CLAUDE.md §2.2.1).

5개 항목 합산 (총 100점). 페널티는 호출자가 별도로 차감한다.
가중치는 ``config/strategy_params.yaml#screening.weights``에서 로드.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from core.indicators import sma


@dataclass(frozen=True)
class ScoringWeights:
    turnover_rank: float = 25.0
    opening_gap: float = 20.0
    ma_alignment: float = 20.0
    sector_theme: float = 15.0
    volatility_atr: float = 20.0


@dataclass(frozen=True)
class ScoreBreakdown:
    total: float
    parts: dict[str, float] = field(default_factory=dict)


def score_turnover_rank(rank: int, *, total: int = 30, weight: float = 25.0) -> float:
    if rank < 1 or rank > total:
        return 0.0
    return weight * (total - rank + 1) / total


def score_opening_gap(open_price: int, prev_close: int, *, weight: float = 20.0) -> float:
    if prev_close <= 0:
        return 0.0
    gap = (open_price - prev_close) / prev_close
    if 0.01 <= gap <= 0.05:
        return weight
    if -0.01 <= gap < 0.01:
        return weight * 0.5
    if 0.05 < gap <= 0.10:
        return weight * 0.5
    return 0.0


def score_ma_alignment(
    closes: Sequence[float],
    periods: tuple[int, int, int] = (5, 20, 60),
    *,
    weight: float = 20.0,
) -> float:
    p_s, p_m, p_l = periods
    if len(closes) < p_l:
        return 0.0
    ma_s = sma(list(closes), p_s)[-1]
    ma_m = sma(list(closes), p_m)[-1]
    ma_l = sma(list(closes), p_l)[-1]
    if ma_s is None or ma_m is None or ma_l is None:
        return 0.0
    if ma_s > ma_m > ma_l:
        return weight
    if ma_s > ma_m or ma_m > ma_l:
        return weight * 0.5
    return 0.0


def score_sector_theme(in_top_themes: bool, *, weight: float = 15.0) -> float:
    return weight if in_top_themes else 0.0


def score_volatility_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    period: int = 20,
    optimal_range: tuple[float, float] = (0.005, 0.04),
    weight: float = 20.0,
) -> float:
    """ATR/last_close 비율이 적정 범위에 들면 만점."""
    if len(closes) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    atr = sum(trs[-period:]) / period
    last_close = closes[-1]
    if last_close <= 0:
        return 0.0
    atr_pct = atr / last_close
    lo, hi = optimal_range
    if lo <= atr_pct <= hi:
        return weight
    if atr_pct < lo or atr_pct > hi * 2:
        return weight * 0.25
    return weight * 0.5


def total_score(
    *,
    rank: int,
    open_price: int,
    prev_close: int,
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    in_top_themes: bool = False,
    weights: ScoringWeights | None = None,
    total_candidates: int = 30,
) -> ScoreBreakdown:
    w = weights or ScoringWeights()
    parts = {
        "turnover_rank": score_turnover_rank(rank, total=total_candidates, weight=w.turnover_rank),
        "opening_gap": score_opening_gap(open_price, prev_close, weight=w.opening_gap),
        "ma_alignment": score_ma_alignment(closes, weight=w.ma_alignment),
        "sector_theme": score_sector_theme(in_top_themes, weight=w.sector_theme),
        "volatility_atr": score_volatility_atr(highs, lows, closes, weight=w.volatility_atr),
    }
    return ScoreBreakdown(total=sum(parts.values()), parts=parts)
