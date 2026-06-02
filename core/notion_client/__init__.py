"""Notion API 연동 (CLAUDE.md 학습부 외부 지식 수집).

학습부(`agents/learning/notion_sync`)가 Notion 페이지 전체를 읽어 매매 전략 지식으로
분류하고, 각 부서 에이전트가 세션 시작 시 참조한다.

- ``NotionClient``       : Notion REST API 비동기 클라이언트(블록 재귀 수집).
- ``NotionConfig``       : ``config/kis_api.yaml``의 ``notion`` 섹션 로더(+env 폴백).
- ``NotionPage``/``NotionSection`` : 수집 결과 모델.
- ``classify_knowledge`` : 페이지 내용을 5개 부서 카테고리로 규칙 기반 분류.
- ``NotionKnowledgeView``: ``data/memory/notion_knowledge.json`` 읽기 인터페이스.
- ``AGENT_CATEGORIES``   : 카테고리 키 ↔ 에이전트 매핑.
"""
from __future__ import annotations

from .classifier import AGENT_CATEGORIES, CATEGORY_LABELS, classify_knowledge
from .client import (
    NotionAuthError,
    NotionClient,
    NotionConfig,
    NotionError,
    NotionPage,
    NotionSection,
)
from .knowledge import NotionKnowledgeView
from .strategy_extract import PendingRule, extract_strategy_rules

__all__ = [
    "AGENT_CATEGORIES",
    "CATEGORY_LABELS",
    "classify_knowledge",
    "NotionAuthError",
    "NotionClient",
    "NotionConfig",
    "NotionError",
    "NotionPage",
    "NotionSection",
    "NotionKnowledgeView",
    "extract_strategy_rules",
    "PendingRule",
]
