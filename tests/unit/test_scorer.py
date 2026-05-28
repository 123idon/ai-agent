"""Unit tests for screening scorer."""
from __future__ import annotations

from agents.intel.screening.scorer import (
    score_ma_alignment,
    score_opening_gap,
    score_sector_theme,
    score_turnover_rank,
    score_volatility_atr,
    total_score,
)


def test_turnover_rank_top_is_max() -> None:
    assert score_turnover_rank(1, total=30) == 25.0
    assert score_turnover_rank(30, total=30) > 0
    assert score_turnover_rank(31, total=30) == 0.0


def test_opening_gap_buckets() -> None:
    assert score_opening_gap(102, 100) == 20.0   # +2% sweet spot
    assert score_opening_gap(100, 100) == 10.0   # flat → partial
    assert score_opening_gap(107, 100) == 10.0   # +7% overshoot → partial
    assert score_opening_gap(95, 100) == 0.0     # below 0
    assert score_opening_gap(115, 100) == 0.0    # way too high


def test_ma_alignment_full_credit_on_uptrend() -> None:
    closes = [100 + i * 0.5 for i in range(70)]  # 단조 상승
    assert score_ma_alignment(closes) == 20.0


def test_ma_alignment_zero_on_downtrend() -> None:
    closes = [200 - i * 0.5 for i in range(70)]
    assert score_ma_alignment(closes) == 0.0


def test_ma_alignment_partial_on_short_data() -> None:
    assert score_ma_alignment([100] * 10) == 0.0


def test_volatility_atr_optimal_range() -> None:
    # 일정한 작은 변동성: tr=0.6 / close=100 = 0.6% (sweet spot)
    closes = [100.0] * 30
    highs = [100.3] * 30
    lows = [99.7] * 30
    s = score_volatility_atr(highs, lows, closes)
    assert s == 20.0


def test_sector_theme_binary() -> None:
    assert score_sector_theme(True) == 15.0
    assert score_sector_theme(False) == 0.0


def test_total_score_aggregates() -> None:
    closes = [10_000 + i * 50 for i in range(70)]
    highs = [c + 30 for c in closes]
    lows = [c - 30 for c in closes]
    score = total_score(
        rank=1,
        open_price=10_200,
        prev_close=10_000,    # +2% gap
        closes=closes,
        highs=highs,
        lows=lows,
        in_top_themes=True,
    )
    assert score.total >= 70
    assert set(score.parts.keys()) == {
        "turnover_rank", "opening_gap", "ma_alignment", "sector_theme", "volatility_atr",
    }
