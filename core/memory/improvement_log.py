"""개선사항 영구 기록 + 효과 추적 (CLAUDE.md §11, §19 확장).

상담(💬)·복기(학습)·노션(§23)에서 나온 **모든 전략 파라미터 변경**을 한 곳에
누적 기록한다. 에이전트는 매 세션 시작 시 이 로그를 읽어 "지난번에 무엇을 왜
바꿨는지"를 기억하고(세션 간 기억 초기화 문제 해결), 변경 전후 성과를 비교해
효과 없는 변경은 롤백 후보로 표시한다.

저장: ``data/memory/improvement_log.json``::

    {"entries": [
       {"id","ts","date","source","key","from","to","reason",
        "expected_effect","mode","commit"},
       ...
    ]}

source: ``consult`` | ``review`` | ``notion`` | ``meta``  (변경의 출처)

본 모듈은 순수 데이터(파일 I/O + 저널 집계)로만 구성되어 Bus·KisClient 의존이
없으므로 단위 테스트가 용이하다. 시각은 호출자가 주입(테스트 결정성)한다.
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

IMPROVEMENT_FILENAME = "improvement_log.json"

# 효과 판정 임계 — 변경 후 평균 손익(%)이 이만큼 나아졌/나빠졌으면 개선/악화로 본다.
_EFFECT_EPS = 0.10  # pnl_pct 절대값 0.10%p


@dataclass
class ImprovementEntry:
    id: str
    ts: str                      # ISO-8601 (호출자 주입)
    date: str                    # YYYYMMDD — 저널 매칭용
    source: str                  # consult | review | notion | meta
    key: str                     # strategy_params.yaml 점(.) 경로
    from_value: Any
    to_value: Any
    reason: str = ""
    expected_effect: str = ""    # 사람용 기대 효과 설명
    mode: str = "paper"
    commit: str | None = None    # git short hash (있으면)
    # 효과 평가 결과(나중에 evaluate_effects가 채움)
    before_pnl: float | None = None
    after_pnl: float | None = None
    before_trades: int = 0
    after_trades: int = 0
    verdict: str | None = None   # improved | worse | flat | unknown

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ImprovementLog:
    path: Path
    entries: list[ImprovementEntry] = field(default_factory=list)

    # ─────────────────────────── 로드/저장 ───────────────────────────

    @classmethod
    def load(cls, memory_dir: Path) -> "ImprovementLog":
        path = Path(memory_dir) / IMPROVEMENT_FILENAME
        entries: list[ImprovementEntry] = []
        if path.exists():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                for e in doc.get("entries", []):
                    entries.append(ImprovementEntry(
                        id=str(e.get("id", "")),
                        ts=str(e.get("ts", "")),
                        date=str(e.get("date", "")),
                        source=str(e.get("source", "")),
                        key=str(e.get("key", "")),
                        from_value=e.get("from"),
                        to_value=e.get("to"),
                        reason=str(e.get("reason", "")),
                        expected_effect=str(e.get("expected_effect", "")),
                        mode=str(e.get("mode", "paper")),
                        commit=e.get("commit"),
                        before_pnl=e.get("before_pnl"),
                        after_pnl=e.get("after_pnl"),
                        before_trades=int(e.get("before_trades", 0)),
                        after_trades=int(e.get("after_trades", 0)),
                        verdict=e.get("verdict"),
                    ))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("improvement_log 로드 실패(%s) — 빈 로그로 시작", exc)
        return cls(path=path, entries=entries)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": [
                {
                    "id": e.id, "ts": e.ts, "date": e.date, "source": e.source,
                    "key": e.key, "from": e.from_value, "to": e.to_value,
                    "reason": e.reason, "expected_effect": e.expected_effect,
                    "mode": e.mode, "commit": e.commit,
                    "before_pnl": e.before_pnl, "after_pnl": e.after_pnl,
                    "before_trades": e.before_trades, "after_trades": e.after_trades,
                    "verdict": e.verdict,
                }
                for e in self.entries
            ],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    # ─────────────────────────── 기록 ───────────────────────────

    def record(
        self,
        *,
        ts: str,
        date: str,
        source: str,
        key: str,
        from_value: Any,
        to_value: Any,
        reason: str = "",
        expected_effect: str = "",
        mode: str = "paper",
        commit: str | None = None,
    ) -> ImprovementEntry:
        entry = ImprovementEntry(
            id=uuid.uuid4().hex[:10], ts=ts, date=date, source=source, key=key,
            from_value=from_value, to_value=to_value, reason=reason,
            expected_effect=expected_effect, mode=mode, commit=commit,
        )
        self.entries.append(entry)
        self.save()
        return entry

    # ─────────────────────────── 조회 ───────────────────────────

    def timeline(self, limit: int | None = None) -> list[dict[str, Any]]:
        """최신순 변경 타임라인(HTS '최근 적용된 변경사항' 패널용)."""
        items = sorted(self.entries, key=lambda e: e.ts, reverse=True)
        if limit is not None:
            items = items[:limit]
        return [e.to_dict() for e in items]

    def last_for_key(self, key: str) -> ImprovementEntry | None:
        matches = [e for e in self.entries if e.key == key]
        if not matches:
            return None
        return max(matches, key=lambda e: e.ts)

    # ─────────────────────────── 효과 평가 ───────────────────────────

    def evaluate_effects(self, journal_dir: Path, *, window_days: int = 3) -> None:
        """각 변경의 전후 평균 손익을 저널에서 집계해 verdict를 채운다.

        변경일(date)을 기준으로 직전 ``window_days``일 vs 직후 ``window_days``일의
        평균 ``signal.exit.pnl_pct``를 비교한다. 거래일 디렉터리(``{YYYYMMDD}.jsonl``)
        파일명을 정렬해 변경일 이전/이후 구간을 잡는다.
        """
        all_dates = sorted(
            p.stem for p in Path(journal_dir).glob("*.jsonl") if p.stem.isdigit()
        )
        for e in self.entries:
            if not e.date:
                continue
            before_dates = [d for d in all_dates if d < e.date][-window_days:]
            after_dates = [d for d in all_dates if d >= e.date][:window_days]
            b_pnl, b_n = _avg_pnl(journal_dir, before_dates)
            a_pnl, a_n = _avg_pnl(journal_dir, after_dates)
            e.before_pnl = round(b_pnl, 4) if b_n else None
            e.after_pnl = round(a_pnl, 4) if a_n else None
            e.before_trades = b_n
            e.after_trades = a_n
            e.verdict = _verdict(e.before_pnl, e.after_pnl, a_n)
        self.save()

    def rollback_candidates(self) -> list[dict[str, Any]]:
        """효과가 악화(worse)로 판정된 변경 — 자동 롤백 제안 대상."""
        out: list[dict[str, Any]] = []
        for e in self.entries:
            if e.verdict == "worse":
                out.append({
                    "id": e.id, "key": e.key,
                    "from": e.from_value, "to": e.to_value,
                    # 롤백 = to→from 으로 되돌림
                    "rollback_to": e.from_value,
                    "before_pnl": e.before_pnl, "after_pnl": e.after_pnl,
                    "reason": (
                        f"변경 후 평균 손익 {e.after_pnl}%(전 {e.before_pnl}%)로 "
                        f"악화 — {e.key} 를 {e.to_value}→{e.from_value} 롤백 제안"
                    ),
                })
        return out

    # ─────────────────────────── 세션 브리프 ───────────────────────────

    def session_brief(self, limit: int = 5) -> str:
        """세션 시작 시 에이전트가 로그로 남길 '기억' 요약(한 줄)."""
        if not self.entries:
            return ""
        recent = sorted(self.entries, key=lambda e: e.ts, reverse=True)[:limit]
        parts = [f"{e.key} {e.from_value}→{e.to_value}" for e in recent]
        return f"기억: 최근 적용 {len(self.entries)}건 — " + " · ".join(parts)


# ─────────────────────────── 저널 집계 헬퍼 ───────────────────────────


def _avg_pnl(journal_dir: Path, dates: list[str]) -> tuple[float, int]:
    pnls: list[float] = []
    for d in dates:
        p = Path(journal_dir) / f"{d}.jsonl"
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("topic") != "signal.exit":
                    continue
                payload = rec.get("payload")
                if isinstance(payload, dict):
                    pnls.append(float(payload.get("pnl_pct", 0.0)))
        except OSError:
            continue
    if not pnls:
        return 0.0, 0
    # pnl_pct는 비율(0.01=1%)일 수도, %일 수도 있으나 일관 비교만 하면 되므로 그대로 평균.
    return sum(pnls) / len(pnls) * 100.0, len(pnls)


def _verdict(before: float | None, after: float | None, after_n: int) -> str:
    if after is None or after_n == 0:
        return "unknown"
    if before is None:
        return "unknown"
    if after - before > _EFFECT_EPS:
        return "improved"
    if before - after > _EFFECT_EPS:
        return "worse"
    return "flat"
