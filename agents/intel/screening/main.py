"""Screening agent (CLAUDE.md §2.2.1).

거래대금 상위 → 차트로 점수 계산 → 70점 이상만 ``screening.candidates`` 발행.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from agents.analysis.signal.indicators import KST
from agents.intel.screening.scorer import ScoringWeights, total_score
from core.kis_client import KisBusinessError, KisClient, KisTransportError
from core.messaging import Bus

log = logging.getLogger(__name__)

TOPIC_CANDIDATES = "screening.candidates"


@dataclass(frozen=True)
class ScreeningCandidate:
    code: str
    name: str
    score: float
    breakdown: dict[str, float]
    timestamp: datetime
    reason: str


@dataclass(frozen=True)
class ScreeningParams:
    threshold: float = 70.0
    top_n: int = 30
    market: str = "0000"            # 전체 (0001 KOSPI / 1001 KOSDAQ)
    rank_by: int = 3                # 거래금액순
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    top_themes: tuple[str, ...] = ()

    @classmethod
    def from_file(cls, path: Path) -> "ScreeningParams":
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        s = doc.get("screening", {})
        w = s.get("weights", {})
        return cls(
            threshold=float(s.get("threshold", 70.0)),
            weights=ScoringWeights(
                turnover_rank=float(w.get("turnover_rank", 25.0)),
                opening_gap=float(w.get("opening_gap", 20.0)),
                ma_alignment=float(w.get("ma_alignment", 20.0)),
                sector_theme=float(w.get("sector_theme", 15.0)),
                volatility_atr=float(w.get("volatility_atr", 20.0)),
            ),
        )


class ScreeningAgent:
    def __init__(
        self,
        kis: KisClient,
        bus: Bus,
        params: ScreeningParams | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(KST),
    ) -> None:
        self._kis = kis
        self._bus = bus
        self._p = params or ScreeningParams()
        self._clock = clock

    async def screen_once(self) -> list[ScreeningCandidate]:
        ranks = await self._kis.get_volume_rank(
            market=self._p.market,
            rank_by=self._p.rank_by,
            top_n=self._p.top_n,
        )
        candidates: list[ScreeningCandidate] = []
        for item in ranks.items:
            try:
                cand = await self._score_one(item.rank, item.code, item.name)
            except (KisBusinessError, KisTransportError) as e:
                log.warning("screening fetch failed: %s (%s)", item.code, e)
                continue
            if cand is None:
                continue
            if cand.score >= self._p.threshold:
                await self._bus.publish(TOPIC_CANDIDATES, cand)
                candidates.append(cand)
        log.info("screening: %d candidates ≥ %.1f", len(candidates), self._p.threshold)
        return candidates

    async def _score_one(
        self, rank: int, code: str, name: str,
    ) -> ScreeningCandidate | None:
        chart = await self._kis.get_chart(code, tf="1")
        if len(chart.candles) < 21:
            return None
        candles = chart.candles
        closes = [float(c.c) for c in candles]
        highs = [float(c.h) for c in candles]
        lows = [float(c.l) for c in candles]
        first = candles[0]
        prev_close = self._infer_prev_close(candles)
        breakdown = total_score(
            rank=rank,
            open_price=int(first.o),
            prev_close=prev_close,
            closes=closes,
            highs=highs,
            lows=lows,
            in_top_themes=False,         # 테마 정보 별도 수집 시 통합 (v1 미연결)
            weights=self._p.weights,
            total_candidates=self._p.top_n,
        )
        reason = ", ".join(f"{k}={v:.1f}" for k, v in breakdown.parts.items())
        return ScreeningCandidate(
            code=code,
            name=name,
            score=breakdown.total,
            breakdown=breakdown.parts,
            timestamp=self._clock(),
            reason=f"{breakdown.total:.1f}/100 ({reason})",
        )

    @staticmethod
    def _infer_prev_close(candles: Sequence) -> int:
        for c in candles:
            if getattr(c, "isPrev", False):
                return int(c.c)
        # prev가 없으면 첫 캔들의 종가를 prev로 간주 (gap 점수 0에 가깝게)
        return int(candles[0].c) if candles else 0
