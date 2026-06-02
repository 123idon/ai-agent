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
from agents.learning.sector import SectorSnapshot
from core.kis_client import KisBusinessError, KisClient, KisTransportError
from core.messaging import Bus
from core.notion_client import NotionKnowledgeView

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
    # 관리종목/거래정지 등 하드 필터(§2.2.1 DART -100). 폴백으로도 부활시키지 않는다.
    hard_filtered: bool = False
    # 스크리닝 시점 현재가(원). 진입 선별기(§5.7)가 가용현금 대비 매수 가능 여부를
    # 판정하는 데 쓴다(1주도 못 사는 종목은 진입 후보에서 제외). 0이면 미상.
    price: int = 0


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
        score_adjust: Callable[[str], float] | None = None,
        sector_provider: Callable[[str], SectorSnapshot] | None = None,
        notion_knowledge: NotionKnowledgeView | None = None,
    ) -> None:
        self._kis = kis
        self._bus = bus
        self._p = params or ScreeningParams()
        self._clock = clock
        self._themes = theme_detector or ThemeDetector()
        self._corp_code_cache: dict[str, str | None] = {}
        # §19 메모리: 종목별 점수 보정(반복 손절 종목 기준 강화). 0 이하 가점.
        self._score_adjust = score_adjust
        # §2.2.1 섹터 강도 가산점(B안): 학습부가 날짜별 전일 섹터 데이터를 반환하면,
        # 종목별 섹터 등락률/대장주 가산점을 최종 점수에 합산한다(데이터/매핑 없으면 0).
        self._sector_provider = sector_provider
        # 학습부 노션 지식(세션 시작 시 참조) — 스크리닝(종목 선정) 기준 카테고리.
        self._notion = notion_knowledge
        if notion_knowledge is not None and notion_knowledge.available:
            note = notion_knowledge.summary_line("intel.screening")
            if note:
                log.info("📚 스크리닝 %s", note)

    async def screen_once(self) -> list[ScreeningCandidate]:
        ranks = await self._kis.get_volume_rank(
            market=self._p.market,
            rank_by=self._p.rank_by,
            top_n=self._p.top_n,
        )
        # §2.2.1 섹터 가산점(B안): 종목 선정 전에 학습부 전일 섹터 데이터를 자동 호출.
        # 학습부 추출기가 절대 예외를 던지지 않으나 방어적으로 한 번 더 감싼다(요구 4).
        sector = self._resolve_sector_snapshot()
        scored: list[ScreeningCandidate] = []
        for item in ranks.items:
            try:
                cand = await self._score_one(item.rank, item.code, item.name, sector)
            except (KisBusinessError, KisTransportError) as e:
                log.warning("screening fetch failed: %s (%s)", item.code, e)
                continue
            if cand is None:
                continue
            scored.append(cand)

        candidates = [
            c for c in scored if c.score >= self._p.threshold and not c.hard_filtered
        ]
        # §19 불변식: 메모리 감점 등으로 임계 통과 후보가 0이어도 "유니버스를 통째로
        # 비우지 않는다". 임계 미달이면 최고점 후보 1개를 폴백으로 내보내 단일 집중(§5.7)
        # 파이프라인이 굶지 않게 한다(하류 신호분석·리스크 게이트가 최종 판정한다).
        # 단 관리종목/거래정지(하드 필터)는 폴백으로도 부활시키지 않는다.
        fallback = False
        if not candidates:
            pool = [c for c in scored if not c.hard_filtered]
            if pool:
                candidates = [max(pool, key=lambda c: c.score)]
                fallback = True

        for cand in candidates:
            await self._bus.publish(TOPIC_CANDIDATES, cand)
        sector_note = ""
        if sector is not None and sector.date:
            top = sector.sectors[0] if sector.sectors else None
            sector_note = (
                f", 섹터데이터 {sector.date}({len(sector.sectors)}섹터"
                + (f", 최강 {top.name} {top.change_pct:+.1f}%" if top else "")
                + ")"
            )
        log.info(
            "screening: %d candidates ≥ %.1f%s%s",
            len(candidates), self._p.threshold,
            " (폴백: 임계 통과 0 → 최고점 1)" if fallback else "",
            sector_note,
        )
        return candidates

    def _resolve_sector_snapshot(self) -> SectorSnapshot | None:
        """종목 선정 전 학습부 전일 섹터 데이터 자동 호출(날짜만 전달). 실패는 비치명."""
        if self._sector_provider is None:
            return None
        try:
            return self._sector_provider(self._clock().strftime("%Y%m%d"))
        except Exception as e:  # noqa: BLE001 — 섹터 데이터 실패는 가산점 0(요구 4)
            log.warning("섹터 데이터 호출 실패(%s) — 가산점 0으로 진행", e)
            return None

    async def _score_one(
        self, rank: int, code: str, name: str,
        sector: SectorSnapshot | None = None,
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

        # §19 장기 메모리: 반복 손절 이력 종목은 점수를 깎아 기준을 강화한다.
        mem_note = ""
        if self._score_adjust is not None:
            adj = self._score_adjust(code)
            if adj:
                final_score += adj
                parts["memory"] = round(adj, 1)
                mem_note = f"기억보정 {adj:+.0f}"

        # §2.2.1 섹터 강도 가산점(B안): 종목 섹터의 전일 등락률/대장주 가산점 합산.
        # 매핑/데이터 없으면 (0, "") → 기존 점수 그대로(요구 4).
        sector_note = ""
        if sector is not None:
            sb, sb_reason = sector.bonus_for(code)
            if sb:
                final_score += sb
                parts["sector_bonus"] = round(sb, 1)
            if sb_reason:
                sector_note = sb_reason

        reason_parts = ", ".join(f"{k}={v:.1f}" for k, v in parts.items())
        reason = f"{final_score:.1f}/100 ({reason_parts})"
        if mem_note:
            reason += f" | {mem_note}"
        if sector_note:
            reason += f" | {sector_note}"
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
            # 관리종목/거래정지류(-100)는 하드 필터 — 폴백 후보에서도 제외.
            hard_filtered=penalty <= -50,
            # 진입 선별기 가용성 판정용 현재가(컷오프 분봉의 최신 종가).
            price=int(closes[-1]) if closes else 0,
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
