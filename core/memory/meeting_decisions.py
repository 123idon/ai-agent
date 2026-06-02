"""회의 결정 영구 기록 + 효과 추적 + 롤백 (CLAUDE.md §11, §24).

전체 팀 회의(🤝)·1:1 상담(💬)에서 **실제로 strategy_params.yaml 에 적용한 변경**을
회의 맥락(언제·어느 회의·어떤 안건)과 함께 누적 기록한다. ``ImprovementLog`` 가 모든
출처(consult/review/notion/meta)의 변경을 평면적으로 모으는 반면, 본 로그는 **회의 단위**로
묶어 "이 회의에서 무엇을 적용했나 → 그 뒤 성과가 어떻게 됐나 → 효과 없으면 롤백"의
타임라인(HTS '🗳 회의 적용 이력' 패널)을 제공한다.

저장: ``data/memory/meeting_decisions.json``::

    {"decisions": [
       {"id","ts","date","meeting_id","meeting_q","meeting_ts",
        "key","label","from","to","reason","source","commit","improvement_id",
        "rolled_back","rollback_of"},
       ...
    ]}

순수 데이터(파일 I/O)로만 구성 — Bus·KisClient 비의존, 시각은 호출자 주입(테스트 결정성).
효과 verdict 는 ``ImprovementLog`` 와 ``improvement_id`` 로 연결해 조회 시 합쳐 보여준다.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MEETING_DECISIONS_FILENAME = "meeting_decisions.json"
_MAX_DECISIONS = 500


@dataclass
class MeetingDecision:
    id: str
    ts: str                       # ISO-8601 (적용 시각, 호출자 주입)
    date: str                     # YYYYMMDD — 효과 매칭용
    meeting_id: str               # 회의 식별자(없으면 ts 기반)
    meeting_q: str                # 회의 안건/질문
    meeting_ts: str               # 회의 시각
    key: str                      # strategy_params.yaml 점(.) 경로
    label: str                    # 화면 표시용 한글 라벨
    from_value: Any = None
    to_value: Any = None
    reason: str = ""
    source: str = "meeting"       # meeting | meeting-rollback
    commit: str | None = None
    improvement_id: str | None = None   # ImprovementLog 연결(효과 verdict 조회용)
    rolled_back: bool = False     # 이 결정이 이후 롤백됐는지
    rollback_of: str | None = None      # 이 결정이 어떤 결정의 롤백인지(있으면 그 id)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["from"] = d.pop("from_value")
        d["to"] = d.pop("to_value")
        return d


@dataclass
class MeetingDecisionLog:
    path: Path
    decisions: list[MeetingDecision] = field(default_factory=list)

    # ─────────────────────────── 로드/저장 ───────────────────────────

    @classmethod
    def load(cls, memory_dir: Path) -> "MeetingDecisionLog":
        path = Path(memory_dir) / MEETING_DECISIONS_FILENAME
        out: list[MeetingDecision] = []
        if path.exists():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                for e in doc.get("decisions", []):
                    out.append(MeetingDecision(
                        id=str(e.get("id", "")),
                        ts=str(e.get("ts", "")),
                        date=str(e.get("date", "")),
                        meeting_id=str(e.get("meeting_id", "")),
                        meeting_q=str(e.get("meeting_q", "")),
                        meeting_ts=str(e.get("meeting_ts", "")),
                        key=str(e.get("key", "")),
                        label=str(e.get("label", "")),
                        from_value=e.get("from"),
                        to_value=e.get("to"),
                        reason=str(e.get("reason", "")),
                        source=str(e.get("source", "meeting")),
                        commit=e.get("commit"),
                        improvement_id=e.get("improvement_id"),
                        rolled_back=bool(e.get("rolled_back", False)),
                        rollback_of=e.get("rollback_of"),
                    ))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("meeting_decisions 로드 실패(%s) — 빈 로그로 시작", exc)
        return cls(path=path, decisions=out)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"decisions": [d.to_dict() for d in self.decisions[-_MAX_DECISIONS:]]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # ─────────────────────────── 기록 ───────────────────────────

    def record(
        self,
        *,
        ts: str,
        date: str,
        meeting_id: str,
        meeting_q: str,
        meeting_ts: str,
        key: str,
        label: str,
        from_value: Any,
        to_value: Any,
        reason: str = "",
        source: str = "meeting",
        commit: str | None = None,
        improvement_id: str | None = None,
        rollback_of: str | None = None,
    ) -> MeetingDecision:
        entry = MeetingDecision(
            id=uuid.uuid4().hex[:10], ts=ts, date=date,
            meeting_id=meeting_id or ts, meeting_q=meeting_q, meeting_ts=meeting_ts,
            key=key, label=label, from_value=from_value, to_value=to_value,
            reason=reason, source=source, commit=commit,
            improvement_id=improvement_id, rollback_of=rollback_of,
        )
        self.decisions.append(entry)
        self.save()
        return entry

    # ─────────────────────────── 조회 ───────────────────────────

    def find(self, decision_id: str) -> MeetingDecision | None:
        for d in self.decisions:
            if d.id == decision_id:
                return d
        return None

    def mark_rolled_back(self, decision_id: str) -> None:
        d = self.find(decision_id)
        if d is not None:
            d.rolled_back = True
            self.save()

    def timeline(self, limit: int | None = None) -> list[dict[str, Any]]:
        """최신순 회의 결정 타임라인(HTS '🗳 회의 적용 이력' 패널용)."""
        items = sorted(self.decisions, key=lambda d: d.ts, reverse=True)
        if limit is not None:
            items = items[:limit]
        return [d.to_dict() for d in items]
