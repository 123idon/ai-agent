"""Theme detector (CLAUDE.md §2.2.1).

v1: 종목명 키워드 매칭. 외부 테마 데이터 소스 미연결.
실제 운영에서는 KRX 업종 분류 / 거래소 테마 인덱스 / 자체 사전과 결합 권장.
"""
from __future__ import annotations

from dataclasses import dataclass, field


_DEFAULT_THEMES: dict[str, tuple[str, ...]] = {
    "2차전지": ("에코프로", "엘앤에프", "포스코퓨처엠", "LG에너지솔루션", "삼성SDI"),
    "AI": ("폴라리스", "솔트룩스", "셀바스", "포바이오", "한글과컴퓨터"),
    "반도체": ("삼성전자", "SK하이닉스", "한미반도체", "리노공업", "원익IPS", "DB하이텍"),
    "바이오": ("셀트리온", "삼성바이오로직스", "유한양행", "HLB", "알테오젠"),
    "조선": ("HD현대중공업", "한화오션", "삼성중공업", "HD현대미포"),
    "원전": ("두산에너빌리티", "한전기술", "한전KPS"),
    "방산": ("한화에어로스페이스", "LIG넥스원", "현대로템", "한국항공우주"),
}


@dataclass(frozen=True)
class ThemeDetector:
    top_themes: tuple[str, ...] = ()
    themes: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(_DEFAULT_THEMES))

    def is_in_top_themes(self, code: str, name: str) -> bool:
        del code
        if not name:
            return False
        active = self.top_themes or tuple(self.themes.keys())
        for theme in active:
            for kw in self.themes.get(theme, ()):
                if kw and kw in name:
                    return True
        return False

    def detect_themes(self, code: str, name: str) -> tuple[str, ...]:
        del code
        if not name:
            return ()
        active = self.top_themes or tuple(self.themes.keys())
        matched: list[str] = []
        for theme in active:
            for kw in self.themes.get(theme, ()):
                if kw and kw in name:
                    matched.append(theme)
                    break
        return tuple(matched)
