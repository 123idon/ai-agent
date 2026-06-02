"""상담(💬) 대화 영구 누적 + 맥락 로드 (CLAUDE.md §11 확장).

상담 탭의 모든 발화(운영자/에이전트)와 그 결과 적용된 변경을 누적 저장해, 다음
상담 세션이 **이전 대화 맥락을 자동으로 이어받게** 한다(세션 간 기억 초기화 해결).
"지난번에 RSI 기준 바꿨는데 결과 어땠어?" 같은 질의를 위해 키별 마지막 변경을
빠르게 찾는다.

저장: ``data/memory/consult_log.json``::

    {"turns": [
       {"ts","role","text","applied":[{"key","from","to"}]},
       ...
    ]}

role: ``operator`` | ``agent``
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CONSULT_FILENAME = "consult_log.json"


@dataclass
class ConsultTurn:
    ts: str
    role: str            # operator | agent
    text: str
    applied: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ConsultLog:
    path: Path
    turns: list[ConsultTurn] = field(default_factory=list)

    @classmethod
    def load(cls, memory_dir: Path) -> "ConsultLog":
        path = Path(memory_dir) / CONSULT_FILENAME
        turns: list[ConsultTurn] = []
        if path.exists():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                for t in doc.get("turns", []):
                    turns.append(ConsultTurn(
                        ts=str(t.get("ts", "")),
                        role=str(t.get("role", "")),
                        text=str(t.get("text", "")),
                        applied=list(t.get("applied", []) or []),
                    ))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("consult_log 로드 실패(%s) — 빈 로그로 시작", exc)
        return cls(path=path, turns=turns)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "turns": [
                {"ts": t.ts, "role": t.role, "text": t.text, "applied": t.applied}
                for t in self.turns
            ],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    def add_turn(
        self, *, ts: str, role: str, text: str,
        applied: list[dict[str, Any]] | None = None,
    ) -> ConsultTurn:
        turn = ConsultTurn(ts=ts, role=role, text=text, applied=list(applied or []))
        self.turns.append(turn)
        self.save()
        return turn

    def recent(self, n: int = 10) -> list[ConsultTurn]:
        return self.turns[-n:]

    def context_brief(self, n: int = 6) -> str:
        """다음 상담 시작 시 자동 로드할 이전 대화 맥락(요약 텍스트)."""
        if not self.turns:
            return ""
        lines: list[str] = []
        for t in self.turns[-n:]:
            who = "운영자" if t.role == "operator" else "에이전트"
            snippet = t.text.strip().replace("\n", " ")
            if len(snippet) > 80:
                snippet = snippet[:77] + "…"
            tag = ""
            if t.applied:
                tag = " [적용: " + ", ".join(
                    f"{a.get('key')} {a.get('from')}→{a.get('to')}" for a in t.applied
                ) + "]"
            lines.append(f"{who}: {snippet}{tag}")
        return "\n".join(lines)

    def last_change_for_key(self, key: str) -> dict[str, Any] | None:
        """키별 마지막 적용 변경 — '지난번에 RSI 바꿨는데 어땠어?' 응답용."""
        for t in reversed(self.turns):
            for a in t.applied:
                if a.get("key") == key:
                    return {"ts": t.ts, **a}
        return None
