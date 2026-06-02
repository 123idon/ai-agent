"""진입 선별기 (CLAUDE.md §5.7).

무보유일 때만 신규 진입하는 단일 집중 운영을 위해:
  - ``is_flat(balance)``  : 보유 포지션이 0일 때만 True (어떤 보유라도 있으면 진입 금지)
  - ``pick(candidates)``  : 그 시점 후보군에서 강세 섹터(테마별 점수 합 상위)에 속한
                            종목을 우선해 **최강 1종목**을 고른다.

특정 종목에 고정되지 않고, 호출 시점마다 후보를 다시 랭킹해 최선을 선택한다.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence

from agents.intel.screening.main import ScreeningCandidate
from core.kis_client import BalanceSnapshot

log = logging.getLogger(__name__)


class EntrySelector:
    def __init__(self, *, strong_theme_top_k: int = 3) -> None:
        self._top_k = strong_theme_top_k

    @staticmethod
    def is_flat(balance: BalanceSnapshot) -> bool:
        """보유 포지션(qty>0)이 하나도 없으면 True."""
        return not any(p.qty > 0 for p in balance.positions)

    def strong_themes(self, candidates: Sequence[ScreeningCandidate]) -> set[str]:
        """후보군의 테마별 점수 합으로 강세 섹터 상위 K개를 산출."""
        theme_score: dict[str, float] = defaultdict(float)
        for c in candidates:
            for theme in c.themes:
                theme_score[theme] += c.score
        ranked = sorted(theme_score.items(), key=lambda kv: kv[1], reverse=True)
        return {theme for theme, _ in ranked[: self._top_k]}

    def rank(
        self, candidates: Sequence[ScreeningCandidate],
    ) -> list[ScreeningCandidate]:
        """강세 섹터 소속 우선 + 점수순 내림차순으로 정렬한 후보 리스트.

        §5.7 단일 집중은 '최강 1종목'을 진입하되, 최강 종목이 신호 미발생/가용현금
        부족으로 진입 불가일 때 다음 강한 종목을 시도할 수 있도록 정렬 리스트를 제공한다.
        """
        if not candidates:
            return []
        strong = self.strong_themes(candidates)

        def key(c: ScreeningCandidate) -> tuple[int, float, str]:
            in_strong = any(t in strong for t in c.themes)
            # 결정성을 위해 동점은 code 역순(작은 code 우선)으로 tie-break
            return (1 if in_strong else 0, c.score, _neg_code(c.code))

        return sorted(candidates, key=key, reverse=True)

    def pick(
        self, candidates: Sequence[ScreeningCandidate],
    ) -> ScreeningCandidate | None:
        """강세 섹터 소속 우선 + 점수순으로 최강 1종목 선택."""
        ranked = self.rank(candidates)
        if not ranked:
            return None
        best = ranked[0]
        strong = self.strong_themes(candidates)
        log.info(
            "SELECT %s score=%.1f themes=%s strong_sectors=%s",
            best.code, best.score, ",".join(best.themes) or "-",
            ",".join(sorted(strong)) or "-",
        )
        return best


def _neg_code(code: str) -> str:
    """code 오름차순을 max()에서 우선하도록 보수치 (작은 code가 큰 키)."""
    # 6자리 숫자코드 가정 — 자리 반전으로 오름차순 우선 구현
    try:
        return f"{999999 - int(code):06d}"
    except ValueError:
        return code
