"""``data/memory/notion_knowledge.json`` 읽기 인터페이스.

각 부서 에이전트가 **세션 시작(생성) 시** 이 뷰를 받아 자신의 카테고리 지식을
참조한다(§19 메모리 뷰와 동일한 사용 패턴). 파일이 없으면 빈 뷰로 동작한다
(노션 미동기화 상태에서도 시스템은 정상 가동 — graceful degradation).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_REL_PATH = Path("data") / "memory" / "notion_knowledge.json"

# 에이전트 식별자(sender) → 카테고리 키. 클래스가 자신의 sender로 조회한다.
_AGENT_TO_CATEGORY = {
    "intel.screening": "screening",
    "analysis.signal": "signal",
    "risk.risk_manager": "risk",
    "intel.market_watch": "market_watch",
    "ceo": "ceo",
    # 짧은 별칭
    "screening": "screening",
    "signal": "signal",
    "risk": "risk",
    "market_watch": "market_watch",
}


class NotionKnowledgeView:
    def __init__(self, data: dict | None) -> None:
        self._data = data or {}

    # ─────────────────────────── 로드 ───────────────────────────

    @classmethod
    def load(cls, project_root: Path) -> "NotionKnowledgeView":
        path = Path(project_root) / DEFAULT_REL_PATH
        return cls.load_path(path)

    @classmethod
    def load_path(cls, path: Path) -> "NotionKnowledgeView":
        p = Path(path)
        if not p.exists():
            return cls(None)
        try:
            return cls(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("notion_knowledge.json 로드 실패: %s", exc)
            return cls(None)

    # ─────────────────────────── 질의 ───────────────────────────

    @property
    def available(self) -> bool:
        return bool(self._data.get("categories"))

    @property
    def title(self) -> str:
        return self._data.get("title", "")

    @property
    def updated_at(self) -> str:
        return self._data.get("fetched_at", "")

    def category(self, key: str) -> dict:
        cat = (self._data.get("categories") or {}).get(key)
        return cat or {"label": "", "rules": [], "headings": [], "count": 0}

    def for_agent(self, agent: str) -> dict:
        """에이전트 식별자(sender)로 해당 카테고리 지식을 반환."""
        key = _AGENT_TO_CATEGORY.get(agent, agent)
        return self.category(key)

    def rules(self, agent: str, *, limit: int | None = None) -> list[dict]:
        rules = self.for_agent(agent).get("rules", [])
        return rules[:limit] if limit else list(rules)

    def summary_line(self, agent: str) -> str:
        cat = self.for_agent(agent)
        n = cat.get("count", 0)
        if not n:
            return ""
        heads = cat.get("headings", [])[:3]
        tail = (" · " + ", ".join(heads)) if heads else ""
        return f"노션 지식 {n}건 [{cat.get('label','')}]{tail}"
