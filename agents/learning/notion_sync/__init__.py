"""학습부 — 노션 지식 동기화 (CLAUDE.md §2.6, §11).

Notion 페이지를 읽어 5개 부서 카테고리로 분류하고
``data/memory/notion_knowledge.json`` 에 저장한다. 변경 감지 시
``data/memory/notion_updates.log`` 에 기록하고 ``learning.notion`` 토픽으로 발행한다.
"""
from .main import NotionSyncAgent

__all__ = ["NotionSyncAgent"]
