"""Unit tests for EntrySelector (CLAUDE.md §5.7)."""
from __future__ import annotations

from datetime import datetime

from agents.execution.selector import EntrySelector
from agents.intel.screening.main import ScreeningCandidate
from core.kis_client import BalanceSnapshot, Position


def _cand(code: str, score: float, themes: tuple[str, ...]) -> ScreeningCandidate:
    return ScreeningCandidate(
        code=code, name=f"종목{code}", score=score, breakdown={},
        themes=themes, timestamp=datetime(2026, 5, 29, 10, 0, 0), reason="t",
    )


def _balance(positions: list[Position]) -> BalanceSnapshot:
    return BalanceSnapshot(cash=10_000_000, totalEval=10_000_000, totalPnl=0, positions=positions)


def _pos(code: str, qty: int) -> Position:
    return Position(
        code=code, name="x", qty=qty, avgPrice=10_000, currentPrice=10_000,
        evalAmt=qty * 10_000, pnl=0,
    )


# ─────────────────────────── pick ───────────────────────────


def test_pick_prefers_strong_sector_over_raw_score() -> None:
    sel = EntrySelector()
    cands = [
        _cand("000001", 80, ("AI",)),
        _cand("000002", 90, ()),         # 높은 점수지만 강세 테마 미소속
        _cand("000003", 75, ("AI",)),
    ]
    best = sel.pick(cands)
    assert best is not None
    assert best.code == "000001"          # 강세 테마(AI) 소속 + 그 중 최고점


def test_strong_themes_by_score_sum() -> None:
    sel = EntrySelector(strong_theme_top_k=1)
    cands = [
        _cand("000001", 71, ("바이오",)),
        _cand("000002", 72, ("AI",)),
        _cand("000003", 73, ("AI",)),
    ]
    # AI = 145, 바이오 = 71 → top1 = {AI}
    assert sel.strong_themes(cands) == {"AI"}


def test_pick_tie_break_smaller_code() -> None:
    sel = EntrySelector()
    cands = [
        _cand("000010", 80, ("AI",)),
        _cand("000005", 80, ("AI",)),     # 동점·동일테마 → 작은 code 우선
    ]
    best = sel.pick(cands)
    assert best is not None
    assert best.code == "000005"


def test_pick_empty_returns_none() -> None:
    assert EntrySelector().pick([]) is None


def test_pick_no_themes_falls_back_to_score() -> None:
    sel = EntrySelector()
    cands = [_cand("000001", 70, ()), _cand("000002", 95, ())]
    best = sel.pick(cands)
    assert best is not None
    assert best.code == "000002"


# ─────────────────────────── is_flat ───────────────────────────


def test_is_flat_true_when_no_positions() -> None:
    assert EntrySelector.is_flat(_balance([])) is True


def test_is_flat_true_when_only_zero_qty() -> None:
    assert EntrySelector.is_flat(_balance([_pos("005930", 0)])) is True


def test_is_flat_false_when_holding() -> None:
    assert EntrySelector.is_flat(_balance([_pos("005930", 10)])) is False
