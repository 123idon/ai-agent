"""Screening agent (CLAUDE.md §2.2.1).

거래대금 상위 → 차트 점수 + 테마/공시 페널티 → 70점 이상만 ``screening.candidates`` 발행.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from agents.analysis.signal.indicators import KST
from agents.intel.screening.scorer import ScoringWeights, dart_penalty, total_score
from agents.intel.screening.theme import ThemeDetector
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
    themes: tuple[str, ...]
    timestamp: datetime
    reason: str


@dataclass(frozen=True)
class ScreeningParams:
    threshold: float = 70.0
    top_n: int = 30
    market: str = "0000"            # 전체 (0001=KOSPI, 1001=KOSDAQ)
    rank_by: int = 3                # 거래금액순
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    enable_dart: bool = True
    dart_lookback_days: int = 2

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
        theme_detector: ThemeDetector | None = None,
    ) -> None:
        self._kis = kis
        self._bus = bus
        self._p = params or ScreeningParams()
        self._clock = clock
        self._themes = theme_detector or ThemeDetector()
        self._corp_code_cache: dict[str, str | None] = {}

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

        themes = self._themes.detect_themes(code, name)
        in_top = len(themes) > 0
        breakdown = total_score(
            rank=rank,
            open_price=int(first.o),
            prev_close=prev_close,
            closes=closes,
            highs=highs,
            lows=lows,
            in_top_themes=in_top,
            weights=self._p.weights,
            total_candidates=self._p.top_n,
        )

        penalty = 0.0
        penalty_reason = ""
        if self._p.enable_dart:
            penalty, penalty_reason = await self._dart_penalty(name)

        final_score = breakdown.total + penalty
        parts = dict(breakdown.parts)
        if penalty < 0:
            parts["dart_penalty"] = penalty

        reason_parts = ", ".join(f"{k}={v:.1f}" for k, v in parts.items())
        reason = f"{final_score:.1f}/100 ({reason_parts})"
        if penalty_reason:
            reason += f" | {penalty_reason}"
        if themes:
            reason += f" | themes={','.join(themes)}"

        return ScreeningCandidate(
            code=code,
            name=name,
            score=final_score,
            breakdown=parts,
            themes=themes,
            timestamp=self._clock(),
            reason=reason,
        )

    async def _dart_penalty(self, name: str) -> tuple[float, str]:
        try:
            corp_code = await self._resolve_corp_code(name)
            if not corp_code:
                return 0.0, ""
            dart = await self._kis.get_dart_list(
                days=self._p.dart_lookback_days, corp_code=corp_code,
            )
        except (KisBusinessError, KisTransportError, Exception) as e:  # noqa: BLE001
            log.debug("DART fetch failed for %s: %s", name, e)
            return 0.0, ""
        reports = [item.report_nm for item in dart.list]
        return dart_penalty(reports)

    async def _resolve_corp_code(self, name: str) -> str | None:
        if name in self._corp_code_cache:
            return self._corp_code_cache[name]
        try:
            code = await self._kis.get_dart_corpcode(name)
        except Exception:  # noqa: BLE001
            code = None
        self._corp_code_cache[name] = code
        return code

    @staticmethod
    def _infer_prev_close(candles: Sequence) -> int:
        for c in candles:
            if getattr(c, "isPrev", False):
                return int(c.c)
        return int(candles[0].c) if candles else 0
