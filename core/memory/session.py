"""세션 시작 시 '영구 기억' 브리프 (CLAUDE.md §11/§19/§23 통합).

에이전트가 매 세션 시작 시 호출해, 이전 세션에서 적용된 개선사항·상담 맥락·노션
반영 내역을 한 번에 떠올린다(세션 간 기억 초기화 문제 해결). 순수 파일 읽기라
부작용이 없고, 파일이 없어도 빈 문자열을 돌려준다.
"""
from __future__ import annotations

import json
from pathlib import Path

from .consult_log import ConsultLog
from .improvement_log import ImprovementLog


def session_learning_brief(memory_dir: Path, *, improvements: int = 5) -> str:
    """세션 시작 로그용 멀티라인 브리프. 비어 있으면 ''."""
    memory_dir = Path(memory_dir)
    lines: list[str] = []

    imp = ImprovementLog.load(memory_dir)
    brief = imp.session_brief(limit=improvements)
    if brief:
        lines.append(brief)
        roll = imp.rollback_candidates()
        if roll:
            keys = ", ".join(r["key"] for r in roll)
            lines.append(f"⚠️ 효과 없는 변경 {len(roll)}건 롤백 검토 필요: {keys}")

    consult = ConsultLog.load(memory_dir)
    if consult.turns:
        lines.append(f"상담 누적 {len(consult.turns)}턴 (맥락 자동 로드됨)")

    notion = _notion_applied_brief(memory_dir)
    if notion:
        lines.append(notion)

    return "\n".join(lines)


def _notion_applied_brief(memory_dir: Path) -> str:
    p = memory_dir / "notion_knowledge.json"
    if not p.exists():
        return ""
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    applied = doc.get("applied_rules") or []
    if not applied:
        return ""
    keys = ", ".join(a.get("key", "") for a in applied[:5])
    return f"노션 반영 규칙 {len(applied)}건: {keys}"
