"""Notion 페이지 내용 → 5개 부서 카테고리 규칙 기반 분류 (LLM 미사용, §15.4·§21).

학습부가 페이지를 읽어 라인 단위로 키워드 매칭해 5개 카테고리로 귀속한다:
- screening    : 스크리닝(종목 선정) 기준        → 스크리닝 에이전트
- signal       : 매수 진입 조건                  → 신호분석 에이전트
- risk         : 손절/익절/리스크 기준           → 리스크 에이전트
- market_watch : 시간대별·시장 환경 전략         → 시장상황 에이전트
- ceo          : 기타 매매 원칙(심리·복기·규칙)  → CEO 에이전트

한 라인이 여러 카테고리 키워드에 걸리면 **점수가 가장 높은** 카테고리로 귀속하고,
동점이면 우선순위(CATEGORY_PRIORITY)로 가른다. 섹션 헤딩이 특정 카테고리를 강하게
가리키면 그 섹션 라인에 가중치를 준다(예: "포지션 청산 전략" 섹션 → risk 가중).
"""
from __future__ import annotations

from .client import NotionPage, NotionSection

# 카테고리 → 담당 에이전트(envelope sender / 식별자)
AGENT_CATEGORIES: dict[str, str] = {
    "screening": "intel.screening",
    "signal": "analysis.signal",
    "risk": "risk.risk_manager",
    "market_watch": "intel.market_watch",
    "ceo": "ceo",
}

CATEGORY_LABELS: dict[str, str] = {
    "screening": "스크리닝(종목 선정) 기준",
    "signal": "매수 진입 조건",
    "risk": "손절/익절·리스크 기준",
    "market_watch": "시간대별·시장 환경 전략",
    "ceo": "기타 매매 원칙",
}

# 동점 시 우선순위(앞이 우선) — 더 구체적/행동적인 카테고리를 앞에 둔다.
CATEGORY_PRIORITY = ("risk", "signal", "screening", "market_watch", "ceo")

# 카테고리별 키워드(가중치). 한국어 단타 커리큘럼 용어 기준.
_KEYWORDS: dict[str, dict[str, int]] = {
    "screening": {
        "스크리닝": 3, "종목 선정": 3, "종목선정": 3, "후보": 2, "거래대금": 3,
        "거래량": 1, "시총": 2, "시가총액": 2, "테마": 2, "재료": 2, "섹터": 2,
        "선도주": 3, "후발주": 2, "동반주": 2, "공시": 2, "유니버스": 3,
        "필터": 2, "관리종목": 2, "거래정지": 2, "상한가": 1, "갭": 1,
        "수급 폭발": 3, "거래량 급증": 3, "300%": 2, "테마주": 3,
    },
    "signal": {
        "진입": 2, "매수": 2, "신호": 2, "타점": 3, "rsi": 3, "macd": 3,
        "이동평균": 3, "이평": 2, "정배열": 3, "볼린저": 3, "vwap": 3, "obv": 3,
        "스토캐스틱": 2, "캔들": 2, "골든크로스": 3, "데드크로스": 2,
        "다이버전스": 3, "돌파": 2, "눌림목": 3, "첫 봉": 2, "첫봉": 2,
        "지표": 2, "히스토그램": 2, "양봉": 1, "망치형": 2, "장대양봉": 2,
        "과매수": 2, "과매도": 2, "밴드워킹": 2, "체결강도": 2, "호가창": 1,
    },
    "risk": {
        "손절": 3, "익절": 3, "청산": 3, "트레일링": 3, "손익비": 3, "r/r": 3,
        "비중": 2, "포지션": 2, "분할": 2, "스탑": 2, "물타기": 2, "피라미딩": 2,
        "보유": 1, "오버나잇": 3, "리스크": 2, "하드 손절": 3, "본전": 1,
        "수수료": 1, "거래세": 1, "목표가": 2, "타임스톱": 3, "쿨다운": 2,
        "-3%": 2, "고점 대비": 2, "손절선": 3, "수익 실현": 2,
    },
    "market_watch": {
        "시간대": 3, "장 전": 2, "장전": 2, "장 중": 2, "장중": 2, "마감": 2,
        "코스피": 2, "코스닥": 2, "나스닥": 2, "s&p": 2, "vix": 3, "환율": 3,
        "선물": 3, "시장 환경": 3, "시장환경": 3, "매크로": 3, "프로그램 매매": 3,
        "외국인": 2, "기관": 2, "지수": 2, "베이시스": 2, "fomc": 2,
        "09:": 2, "14:30": 3, "15:20": 3, "15:25": 2, "야간 선물": 3,
        "공매도": 2, "리밸런싱": 2, "msci": 2,
    },
    "ceo": {
        "원칙": 3, "심리": 3, "체크리스트": 2, "규칙": 2, "복기": 3, "매매 일지": 3,
        "매매일지": 3, "멘탈": 3, "fomo": 2, "복수": 2, "감정": 2, "철학": 2,
        "생존": 2, "기록": 1, "마인드": 2, "습관": 2, "원장": 1,
        "no-go": 2, "go 조건": 2, "self": 1, "절제": 2,
    },
}

# 섹션 헤딩이 강하게 가리키는 카테고리(헤딩 키워드 → 카테고리). 해당 섹션 라인에 +1 가중.
_SECTION_BIAS: dict[str, str] = {
    "스크리닝": "screening", "종목": "screening", "재료": "screening", "테마": "screening",
    "수급": "screening", "차트": "signal", "보조지표": "signal", "기술적": "signal",
    "매매 기법": "signal", "진입": "signal", "청산": "risk", "손절": "risk",
    "익절": "risk", "리스크": "risk", "포지션": "risk", "시장": "market_watch",
    "심리": "ceo", "원칙": "ceo", "복기": "ceo", "통합 점검": "screening",
}


def _section_bias(heading: str) -> str | None:
    low = heading.lower()
    for kw, cat in _SECTION_BIAS.items():
        if kw.lower() in low:
            return cat
    return None


def _score_line(line: str, bias: str | None) -> dict[str, int]:
    low = line.lower()
    scores: dict[str, int] = {}
    for cat, kws in _KEYWORDS.items():
        s = 0
        for kw, w in kws.items():
            if kw in low:
                s += w
        if s:
            scores[cat] = s
    if bias and bias in scores:
        scores[bias] += 1
    elif bias and scores:
        # 헤딩이 강하게 가리키지만 라인 자체엔 키워드가 없을 때 약한 귀속
        scores[bias] = scores.get(bias, 0)
    return scores


def _best_category(scores: dict[str, int]) -> str | None:
    if not scores:
        return None
    best = max(scores.values())
    if best <= 0:
        return None
    tied = [c for c, s in scores.items() if s == best]
    if len(tied) == 1:
        return tied[0]
    for cat in CATEGORY_PRIORITY:
        if cat in tied:
            return cat
    return tied[0]


def _classify_section(
    section: NotionSection,
    buckets: dict[str, dict],
    *,
    max_rules_per_cat: int,
) -> None:
    bias = _section_bias(section.heading)
    # 헤딩 자체도 분류 대상(요약/소스로 기록)
    head_cat = _best_category(_score_line(section.heading, bias)) or bias
    if head_cat:
        heads = buckets[head_cat]["headings"]
        if section.heading and section.heading not in heads:
            heads.append(section.heading)
    for line in section.lines:
        stripped = line.strip("•1.[] ▸>💡").strip()
        if len(stripped) < 4:
            continue
        cat = _best_category(_score_line(line, bias))
        if cat is None:
            continue
        rules = buckets[cat]["rules"]
        if len(rules) >= max_rules_per_cat:
            continue
        # 중복 라인 제거
        if any(r["text"] == stripped for r in rules):
            continue
        rules.append({"text": stripped, "source": section.heading})


def classify_knowledge(page: NotionPage, *, max_rules_per_cat: int = 60) -> dict:
    """페이지를 5개 카테고리로 분류해 지식 dict를 만든다.

    반환 구조::

        {
          "categories": {
             "screening": {"agent","label","headings":[...],"rules":[{"text","source"}],"count"},
             ...
          },
          "stats": {"total_rules", <cat>: count, ...},
        }
    """
    buckets: dict[str, dict] = {
        cat: {"agent": AGENT_CATEGORIES[cat], "label": CATEGORY_LABELS[cat],
              "headings": [], "rules": []}
        for cat in AGENT_CATEGORIES
    }
    for section in page.sections:
        _classify_section(section, buckets, max_rules_per_cat=max_rules_per_cat)

    categories: dict[str, dict] = {}
    stats: dict[str, int] = {}
    total = 0
    for cat, b in buckets.items():
        count = len(b["rules"])
        total += count
        stats[cat] = count
        categories[cat] = {
            "agent": b["agent"],
            "label": b["label"],
            "headings": b["headings"][:20],
            "rules": b["rules"],
            "count": count,
        }
    stats["total_rules"] = total
    return {"categories": categories, "stats": stats}
