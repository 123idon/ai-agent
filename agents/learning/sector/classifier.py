"""종목 → 섹터 분류기 (CLAUDE.md §2.2.1 섹터 가산점).

매핑 우선순위:
1. ``config/sectors.json`` 의 코드별 매핑(권위) — 있으면 임베디드 기본값을 덮어쓴다.
2. 임베디드 기본 매핑(``_DEFAULT_SECTOR_BY_CODE``) — config 파일이 없거나 일부만 채워졌을 때.
3. 종목명 키워드 폴백(``_SECTOR_KEYWORDS``) — 코드 미등록 종목을 종목명으로 추정(live 확장용).

어떤 경우에도 매핑이 안 되면 ``None`` 을 반환하며, 스크리닝은 섹터 가산점을 0으로 둔다
(기존 점수 그대로 — §2.2.1 에러 처리 요건). 분류기는 절대 예외를 던지지 않는다.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# 임베디드 기본 매핑(config/sectors.json 부재/누락 시 폴백). 수집 유니버스 44종목 커버.
_DEFAULT_SECTOR_BY_CODE: dict[str, str] = {
    "005930": "반도체", "000660": "반도체", "058470": "반도체",
    "357780": "반도체", "240810": "반도체",
    "373220": "2차전지", "006400": "2차전지", "003670": "2차전지",
    "247540": "2차전지", "086520": "2차전지", "066970": "2차전지",
    "207940": "바이오", "068270": "바이오", "196170": "바이오",
    "028300": "바이오", "068760": "바이오", "145020": "바이오",
    "005380": "자동차", "000270": "자동차", "012330": "자동차",
    "105560": "금융", "055550": "금융", "086790": "금융", "316140": "금융",
    "032830": "보험",
    "005490": "철강소재", "010130": "철강소재",
    "035420": "인터넷", "035720": "인터넷",
    "051910": "화학",
    "066570": "가전",
    "015760": "전력유틸리티",
    "033780": "필수소비재",
    "011200": "해운",
    "096770": "에너지정유", "010950": "에너지정유",
    "009150": "전자부품",
    "028260": "지주상사", "034730": "지주상사",
    "018260": "IT서비스", "022100": "IT서비스",
    "293490": "게임엔터", "035900": "게임엔터", "041510": "게임엔터",
}

# 종목명 키워드 폴백(코드 미등록 종목 — live 유니버스 확장 대비). 첫 매칭 채택.
_SECTOR_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("반도체", ("반도체", "하이닉스", "DB하이텍", "SK스퀘어")),
    ("2차전지", ("에코프로", "엘앤에프", "퓨처엠", "에너지솔루션", "SDI", "천보", "코스모")),
    ("바이오", ("바이오", "셀트리온", "제약", "양행", "HLB", "알테오젠", "휴젤", "메디", "헬스")),
    ("자동차", ("현대차", "기아", "모비스", "만도", "현대위아")),
    ("게임엔터", ("게임", "엔터", "JYP", "에스엠", "하이브", "넷마블", "엔씨")),
    ("인터넷", ("NAVER", "카카오", "더존")),
    ("금융", ("금융", "지주", "은행", "증권", "캐피탈")),
    ("보험", ("생명", "화재", "손해보험", "해상")),
    ("철강소재", ("POSCO", "포스코홀딩스", "고려아연", "철강", "현대제철")),
    ("에너지정유", ("이노베이션", "S-Oil", "GS", "정유", "에너지")),
    ("화학", ("화학", "케미칼", "롯데정밀")),
    ("조선", ("중공업", "오션", "조선", "미포")),
    ("방산", ("에어로", "넥스원", "로템", "항공우주")),
)


class SectorClassifier:
    """6자리 종목코드(권위) + 종목명 키워드(폴백)로 섹터를 판정한다."""

    def __init__(self, by_code: dict[str, str] | None = None) -> None:
        self._by_code: dict[str, str] = dict(_DEFAULT_SECTOR_BY_CODE)
        if by_code:
            self._by_code.update(by_code)

    @classmethod
    def from_file(cls, path: Path) -> "SectorClassifier":
        """``config/sectors.json`` 로드(없으면 임베디드 기본값). 절대 예외 없음."""
        by_code: dict[str, str] = {}
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
            raw = doc.get("sectors", {})
            by_code = {str(k): str(v) for k, v in raw.items() if k and v}
        except FileNotFoundError:
            log.info("sectors.json 없음 — 임베디드 기본 섹터 매핑 사용: %s", path)
        except Exception as e:  # noqa: BLE001 — 손상 파일도 백테스트를 막지 않는다
            log.warning("sectors.json 로드 실패(%s) — 임베디드 기본 매핑 사용", e)
        return cls(by_code)

    def sector_of(self, code: str, name: str = "") -> str | None:
        """종목 → 섹터명. 미분류면 None(스크리닝은 가산점 0 처리)."""
        if code and code in self._by_code:
            return self._by_code[code]
        if name:
            for sector, keywords in _SECTOR_KEYWORDS:
                for kw in keywords:
                    if kw and kw in name:
                        return sector
        return None

    @property
    def sectors(self) -> tuple[str, ...]:
        return tuple(sorted(set(self._by_code.values())))
