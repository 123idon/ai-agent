"""학습부 — 섹터 데이터 추출 + 섹터 강도 가산점 (CLAUDE.md §2.2.1, §2.6).

목표(B안): 스크리닝 점수에 **섹터 강도 가산점**을 더해 종목 선정 정확도를 높인다.

흐름: 백테스트 날짜 선택 → 학습부가 **전일 종가 기준** 섹터 등락률 산출 → 스크리닝이
``screen_once`` 직전 자동 호출해 ``SectorSnapshot`` 을 받아 종목별 가산점을 합산한다.

데이터 소스: 보유한 로컬 분봉(``CandleStore`` 일봉 집계, §18). 룩어헤드 없음 — 백테스트
날짜 D 의 '전일' = D 이전 가장 가까운 데이터 보유 거래일 P 이며, P 등락률은 P 와 그 직전
거래일 종가로만 계산한다(둘 다 D 이전).

가산점 규칙(요구 3):
- 섹터 등락률: ``+2%↑ → +5`` / ``+1~2% → +3`` / ``-1~+1% → 0`` / ``-1%↓ → -2``
- 섹터 대장주(거래대금 top5): ``+2``

에러 처리(요구 4): 섹터 데이터 없음/매핑 안 됨 → 가산점 0(기존 점수 그대로).
어떤 경우에도 예외를 밖으로 던지지 않아 백테스트를 중단시키지 않는다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.learning.sector.classifier import SectorClassifier

log = logging.getLogger(__name__)

# 등락률 → 가산점 구간 (요구 3).
_TIER_STRONG = (2.0, 5.0)    # +2% 이상 → +5
_TIER_MILD = (1.0, 3.0)      # +1~2%   → +3
_TIER_WEAK = (-1.0, -2.0)    # -1% 이하 → -2 (그 사이 -1~+1% 는 0)
_LEADER_BONUS = 2.0          # 섹터 대장주(top5) → +2
_TOP_N = 5


@dataclass(frozen=True)
class SectorInfo:
    name: str
    change_pct: float
    top5_stocks: tuple[str, ...]   # 종목명(없으면 코드)
    top5_codes: tuple[str, ...]
    member_count: int

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "change_pct": round(self.change_pct, 2),
            "top5_stocks": list(self.top5_stocks),
        }


@dataclass(frozen=True)
class SectorSnapshot:
    """전일 종가 기준 섹터 데이터 + 종목별 가산점 조회."""

    date: str                                  # 전일(데이터 보유) 날짜 YYYYMMDD, 미상이면 ""
    sectors: tuple[SectorInfo, ...] = ()
    _sector_by_code: dict[str, str] = field(default_factory=dict)
    _change_by_sector: dict[str, float] = field(default_factory=dict)
    _leader_codes: frozenset[str] = field(default_factory=frozenset)

    def sector_of(self, code: str) -> str | None:
        return self._sector_by_code.get(code)

    def bonus_for(self, code: str) -> tuple[float, str]:
        """종목 → (가산점, 사유). 매핑/데이터 없으면 (0.0, "")."""
        sector = self._sector_by_code.get(code)
        if sector is None:
            return 0.0, ""
        chg = self._change_by_sector.get(sector)
        if chg is None:
            return 0.0, ""
        if chg >= _TIER_STRONG[0]:
            tier = _TIER_STRONG[1]
        elif chg >= _TIER_MILD[0]:
            tier = _TIER_MILD[1]
        elif chg > _TIER_WEAK[0]:
            tier = 0.0
        else:
            tier = _TIER_WEAK[1]
        leader = _LEADER_BONUS if code in self._leader_codes else 0.0
        total = tier + leader
        if total == 0.0 and leader == 0.0:
            # 가산점이 0이어도 어느 섹터였는지 사유로 남겨 추적성 보장.
            return 0.0, f"섹터 {sector} {chg:+.1f}% → +0"
        parts = [f"섹터 {sector} {chg:+.1f}% → {tier:+.0f}"]
        if leader:
            parts.append("대장주 +2")
        return total, " · ".join(parts)

    def to_dict(self) -> dict:
        """GET /learning/sector-data 응답 형식(요구 1)."""
        return {
            "date": self.date,
            "sectors": [s.to_dict() for s in self.sectors],
        }


class SectorDataProvider:
    """학습부 섹터 데이터 추출기.

    ``sector_data(date)`` 가 진입점이며, 스크리닝(``sector_provider`` 콜백)과
    HTTP/CLI(GET /learning/sector-data?date=) 양쪽에서 호출된다. 같은 날짜 재호출은
    캐시 반환(백테스트 핫루프 보호).
    """

    def __init__(
        self,
        store: Any,
        names: dict[str, str] | None = None,
        classifier: SectorClassifier | None = None,
    ) -> None:
        self._store = store                       # CandleStore (duck-typed)
        self._names = names or {}
        self._cls = classifier or SectorClassifier()
        self._cache: dict[str, SectorSnapshot] = {}

    def sector_data(self, date: str) -> SectorSnapshot:
        """백테스트 날짜 ``date``(YYYYMMDD) 의 **전일 종가 기준** 섹터 스냅샷.

        절대 예외를 던지지 않는다 — 실패 시 빈 스냅샷(가산점 0)을 반환한다.
        """
        date = str(date)
        cached = self._cache.get(date)
        if cached is not None:
            return cached
        try:
            snap = self._compute(date)
        except Exception as e:  # noqa: BLE001 — 어떤 경우에도 백테스트 중단 금지(요구 4)
            log.warning("섹터 데이터 추출 실패(%s) — 가산점 0 폴백", e)
            snap = SectorSnapshot(date="")
        self._cache[date] = snap
        return snap

    def _compute(self, date: str) -> SectorSnapshot:
        if self._store is None:
            return SectorSnapshot(date="")
        avail = [d for d in self._store.available_dates() if d < date]
        if len(avail) < 1:
            return SectorSnapshot(date="")
        prev_day = avail[-1]                       # 전일(데이터 보유 거래일)
        prev_prev = avail[-2] if len(avail) >= 2 else None

        # 종목별: (등락률, 거래대금, 섹터). 섹터 미분류·데이터 부족은 제외.
        per_sector: dict[str, list[tuple[str, float, int]]] = {}
        sector_by_code: dict[str, str] = {}
        for code in self._store.symbols_on(prev_day):
            if not code or str(code).startswith("^"):
                continue
            sector = self._cls.sector_of(code, self._names.get(code, ""))
            if sector is None:
                continue
            agg = self._store.daily_aggregate(prev_day, code)
            if not agg:
                continue
            close = float(agg.get("c") or 0)
            vol = int(agg.get("v") or 0)
            turnover = int(close * vol)
            chg = 0.0
            if prev_prev is not None:
                prev_agg = self._store.daily_aggregate(prev_prev, code)
                if prev_agg:
                    prev_close = float(prev_agg.get("c") or 0)
                    if prev_close > 0:
                        chg = (close - prev_close) / prev_close * 100.0
            sector_by_code[code] = sector
            per_sector.setdefault(sector, []).append((code, chg, turnover))

        sectors: list[SectorInfo] = []
        change_by_sector: dict[str, float] = {}
        leader_codes: set[str] = set()
        for sector, members in per_sector.items():
            avg_chg = sum(m[1] for m in members) / len(members)
            change_by_sector[sector] = avg_chg
            # 대장주 = 거래대금 상위 top5.
            top = sorted(members, key=lambda m: m[2], reverse=True)[:_TOP_N]
            top_codes = tuple(m[0] for m in top)
            leader_codes.update(top_codes)
            top_names = tuple(self._names.get(c, c) for c in top_codes)
            sectors.append(SectorInfo(
                name=sector, change_pct=avg_chg,
                top5_stocks=top_names, top5_codes=top_codes,
                member_count=len(members),
            ))
        sectors.sort(key=lambda s: s.change_pct, reverse=True)

        return SectorSnapshot(
            date=prev_day,
            sectors=tuple(sectors),
            _sector_by_code=sector_by_code,
            _change_by_sector=change_by_sector,
            _leader_codes=frozenset(leader_codes),
        )

    @classmethod
    def from_root(
        cls, root: Path, store: Any, names: dict[str, str] | None = None,
    ) -> "SectorDataProvider":
        classifier = SectorClassifier.from_file(root / "config" / "sectors.json")
        return cls(store, names=names, classifier=classifier)
