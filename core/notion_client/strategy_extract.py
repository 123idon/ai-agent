"""노션 지식 → 전략 파라미터 변경 추출 (CLAUDE.md §23 확장, §13).

``classify_knowledge`` 가 만든 분류 dict(노션 규칙 텍스트)를 훑어, 상담 파서
(``core.consult.extract_changes``)를 재사용해 화이트리스트 키 변경을 뽑는다. 노션에
명시된 규칙은 전략에 **우선 적용**(§요구)되므로, 추출 결과는 그대로 ``StrategyEditor``
로 반영된다. 단 하드리밋(§4)은 화이트리스트 밖이라 추출 단계에서 자연히 배제된다.

아직 파라미터화되지 않은 고급 규칙(R/R 게이트·VWAP·OBV 등)은 ``pending`` 으로 분리해
HTS "미반영 항목(도입 예정)" 패널에 노출한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.consult import Suggestion, extract_changes

# 파라미터화 안 된(=현재 화이트리스트 밖) 고급 규칙 키워드 → 도입 예정 라벨.
_PENDING_KEYWORDS: dict[str, str] = {
    "vwap": "VWAP 기준선 진입 필터",
    "r/r": "손익비(R/R) 게이트",
    "손익비": "손익비(R/R) 게이트",
    "obv": "OBV 수급 지표",
    "스토캐스틱": "스토캐스틱 보조지표",
    "다이버전스": "다이버전스 진입 신호",
    "체결강도": "체결강도 필터",
    "프로그램 매매": "프로그램 매매 잔량 감시",
    "볼린저": "볼린저밴드 수축(스퀴즈) 돌파",
    "스퀴즈": "볼린저밴드 수축(스퀴즈) 돌파",
    "수축": "볼린저밴드 수축(스퀴즈) 돌파",
    "재료": "재료(뉴스·공시) 강도 필터",
    "호재": "재료(뉴스·공시) 강도 필터",
    "테마 강도": "테마 강도 필터",
}


@dataclass(frozen=True)
class PendingRule:
    label: str
    sample: str       # 근거가 된 노션 원문


def extract_strategy_rules(
    knowledge: dict[str, Any],
) -> tuple[list[Suggestion], list[PendingRule]]:
    """노션 분류 dict → (적용 가능한 변경 제안, 미반영 고급 규칙)."""
    suggestions: dict[str, Suggestion] = {}
    pending: dict[str, PendingRule] = {}

    cats = knowledge.get("categories") or {}
    for cat in cats.values():
        for rule in cat.get("rules", []) or []:
            text = str(rule.get("text", ""))
            if not text:
                continue
            for s in extract_changes(text):
                suggestions[s.key] = s   # 노션 우선 — 마지막(가장 구체) 값 채택
            low = text.lower()
            for kw, label in _PENDING_KEYWORDS.items():
                if kw in low and label not in pending:
                    pending[label] = PendingRule(label=label, sample=text[:80])

    return list(suggestions.values()), list(pending.values())
