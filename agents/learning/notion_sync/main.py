"""학습부 노션 동기화 에이전트 (CLAUDE.md §2.6 복기기록·외부 지식 수집, §11).

작동(요구사항):
  1. Notion API로 페이지 전체(+자식 페이지) 읽기            → ``NotionClient.fetch_page``
  2. 내용을 5개 카테고리로 분류                              → ``classify_knowledge``
     - 스크리닝 기준 → 스크리닝 / 매수 진입 조건 → 신호분석
     - 손절·익절 → 리스크 / 시간대별 → 시장상황 / 기타 원칙 → CEO
  3. ``data/memory/notion_knowledge.json`` 저장(원자적)
  4. 각 에이전트는 세션 시작 시 ``NotionKnowledgeView`` 로 참조(별도 와이어링)
  5. 주기적(매일 1회) 업데이트 확인 — 콘텐츠 해시 변경 시에만 재기록 +
     ``data/memory/notion_updates.log`` 에 변경 내역 append + ``learning.notion`` 발행
  6. traidair 상담 탭 "노션 학습 현황"은 ``status()`` 결과(JSON 파일)를 서빙

학습부 원칙(§11): 데이터는 읽기 전용으로 수집할 뿐, **전략 파라미터를 직접 바꾸지 않는다**.
노션 지식은 각 에이전트의 *참고 자료(넛지)* 로만 쓰인다(§19 메모리와 동일 위상).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from core.notion_client import (
    NotionClient,
    NotionConfig,
    NotionError,
    classify_knowledge,
    extract_strategy_rules,
)

try:  # 버스는 선택(스크립트 단독 실행 시 없을 수 있음)
    from core.messaging import Bus
except Exception:  # noqa: BLE001
    Bus = Any  # type: ignore

log = logging.getLogger(__name__)

TOPIC_NOTION = "learning.notion"
KNOWLEDGE_FILENAME = "notion_knowledge.json"
UPDATES_LOG_FILENAME = "notion_updates.log"


def _utcnow_iso() -> str:
    # SimClock/clock 주입을 받지만, 동기화는 실시간(외부 API) 작업이라 벽시계 허용.
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


class NotionSyncAgent:
    def __init__(
        self,
        config: NotionConfig,
        *,
        memory_dir: Path,
        bus: "Bus | None" = None,
        clock: Callable[[], str] = _utcnow_iso,
        client_factory: Callable[[NotionConfig], NotionClient] | None = None,
    ) -> None:
        self._cfg = config
        self._dir = Path(memory_dir)
        self._bus = bus
        self._clock = clock
        self._client_factory = client_factory or (lambda c: NotionClient(c))

    @property
    def knowledge_path(self) -> Path:
        return self._dir / KNOWLEDGE_FILENAME

    @property
    def log_path(self) -> Path:
        return self._dir / UPDATES_LOG_FILENAME

    # ─────────────────────────── 1~3: 읽기·분류·저장 ───────────────────────────

    async def sync(self, *, force: bool = False) -> dict:
        """페이지를 읽어 분류·저장한다. 변경 없으면(force=False) 재기록을 생략.

        반환: {ok, changed, reason?, title, fetched_at, content_hash, stats{...}}
        """
        try:
            async with self._client_factory(self._cfg) as client:
                page = await client.fetch_page(self._cfg.page_id)
        except NotionError as exc:
            # 폴백: 노션 접근 실패 시 기존에 저장된 notion_knowledge.json 을 그대로
            # 재사용한다(버튼이 하드 실패하지 않고 직전 지식을 유지). 저장본이 없을
            # 때만 종전대로 실패를 보고한다.
            prev = self._load_existing()
            if prev:
                now = self._clock()
                prev["last_checked"] = now
                prev["fallback"] = True
                prev["fallback_reason"] = str(exc)
                self._atomic_write(self.knowledge_path, prev)
                self._append_log(f"FALLBACK {exc} → 기존 저장본 사용")
                log.warning("NOTION_SYNC 폴백(기존 저장본 사용): %s", exc)
                return {
                    "ok": True,
                    "changed": False,
                    "fallback": True,
                    "reason": f"노션 접근 실패 → 기존 저장본 사용: {exc}",
                    "title": prev.get("title", ""),
                    "fetched_at": prev.get("fetched_at", ""),
                    "content_hash": prev.get("content_hash", ""),
                    "stats": prev.get("stats", {}),
                }
            log.warning("NOTION_SYNC 실패: %s", exc)
            self._append_log(f"ERROR {exc}")
            return {"ok": False, "changed": False, "error": str(exc)}

        classified = classify_knowledge(page)
        content_hash = self._hash(page.markdown)
        now = self._clock()

        prev = self._load_existing()
        prev_hash = prev.get("content_hash") if prev else None
        changed = force or (prev_hash != content_hash)

        knowledge = {
            "source_page_id": page.page_id,
            "title": page.title,
            "fetched_at": now,
            "content_hash": content_hash,
            "content_length": len(page.markdown),
            "line_count": page.line_count,
            "categories": classified["categories"],
            "stats": classified["stats"],
        }

        if not changed:
            # 마지막 확인 시각만 갱신(내용 동일) — last_checked 필드.
            if prev is not None:
                prev["last_checked"] = now
                self._atomic_write(self.knowledge_path, prev)
            log.info("NOTION_SYNC: 변경 없음 (hash=%s…)", content_hash[:8])
            return {
                "ok": True, "changed": False, "reason": "변경 없음",
                "title": page.title, "fetched_at": now,
                "content_hash": content_hash, "stats": classified["stats"],
            }

        knowledge["last_checked"] = now
        self._atomic_write(self.knowledge_path, knowledge)
        self._write_update_log(prev_hash, content_hash, classified["stats"], now, page.title)

        if self._bus is not None:
            try:
                await self._bus.publish(TOPIC_NOTION, knowledge)
            except Exception:  # noqa: BLE001
                log.debug("learning.notion 발행 실패", exc_info=True)

        log.info(
            "NOTION_SYNC 반영: '%s' — 규칙 %d건 (스크리닝 %d/신호 %d/리스크 %d/시장 %d/CEO %d)",
            page.title, classified["stats"]["total_rules"],
            classified["stats"]["screening"], classified["stats"]["signal"],
            classified["stats"]["risk"], classified["stats"]["market_watch"],
            classified["stats"]["ceo"],
        )
        return {
            "ok": True, "changed": True,
            "title": page.title, "fetched_at": now,
            "content_hash": content_hash, "stats": classified["stats"],
        }

    # ─────────────────────────── 4b: 전략 자동 반영 (§23 요구) ───────────────────────────

    def apply_to_strategy(self, editor: Any, *, ts: str, date: str) -> dict:
        """노션 규칙을 ``strategy_params.yaml`` 에 우선 반영(§23 요구 1·3).

        - 노션에 명시된 화이트리스트 키는 ``StrategyEditor`` 로 적용(노션 우선).
        - 하드리밋(§4)은 화이트리스트 밖이라 자연 배제(노션도 덮어쓰기 불가).
        - 직전에 상담/복기로 다르게 바꾼 키는 **충돌(conflict)** 로 표시(노션이 우선
          적용되지만 운영자에게 알린다).
        - 미반영 고급 규칙(R/R·VWAP 등)은 ``pending`` 으로 분리.
        결과를 ``notion_knowledge.json`` 에 기록하고 요약 dict를 반환한다.
        """
        data = self._load_existing()
        if not data:
            return {"ok": False, "reason": "노션 지식이 없습니다 — 먼저 동기화하세요.",
                    "applied": [], "conflicts": [], "pending": []}

        suggestions, pending = extract_strategy_rules(data)
        imp = getattr(editor, "improvement_log", None)

        applied: list[dict] = []
        conflicts: list[dict] = []
        aligned: list[dict] = []
        failed: list[dict] = []
        for s in suggestions:
            # 충돌 감지: 직전 상담/복기 변경이 노션 값과 다르면 표시(노션 우선 적용).
            if imp is not None:
                prev = imp.last_for_key(s.key)
                if prev is not None and prev.source in ("consult", "review") \
                        and not _eq(prev.to_value, s.value):
                    conflicts.append({
                        "key": s.key, "notion": s.value,
                        "current_source": prev.source, "current": prev.to_value,
                        "note": f"노션('{s.label}')과 {prev.source} 설정 충돌 — 노션 우선 적용",
                    })
            res = editor.apply(
                s.key, s.value, ts=ts, date=date, source="notion",
                reason=f"노션 우선 반영: {s.reason}", label=s.label,
            )
            d = res.to_dict() if hasattr(res, "to_dict") else dict(res)
            if d.get("ok"):
                applied.append(d)
            elif "이미" in str(d.get("reason", "")):
                aligned.append(d)
            else:
                failed.append(d)

        # 결과를 지식 파일에 기록(상담 탭 노출용).
        data["applied_rules"] = [
            {"key": a["key"], "from": a["from"], "to": a["to"], "display": a.get("display", "")}
            for a in applied
        ]
        data["aligned_rules"] = [a["key"] for a in aligned]
        data["pending_rules"] = [{"label": p.label, "sample": p.sample} for p in pending]
        data["conflicts"] = conflicts
        data["strategy_applied_at"] = ts
        self._atomic_write(self.knowledge_path, data)

        self._append_log(
            f"STRATEGY 반영 {len(applied)}건 적용 · 정합 {len(aligned)} · "
            f"충돌 {len(conflicts)} · 미반영 {len(pending)}"
        )
        log.info(
            "NOTION_STRATEGY: 적용 %d · 정합 %d · 충돌 %d · 미반영 %d",
            len(applied), len(aligned), len(conflicts), len(pending),
        )
        return {
            "ok": True, "applied": applied, "aligned": aligned,
            "conflicts": conflicts, "failed": failed,
            "pending": [{"label": p.label, "sample": p.sample} for p in pending],
        }

    # ─────────────────────────── 6: 현황 조회 ───────────────────────────

    def status(self, *, log_tail: int = 20) -> dict:
        """상담 탭 "노션 학습 현황"용 요약.

        - synced: 동기화 여부
        - last_update / last_checked: 마지막 반영/확인 시각
        - title, page_id
        - agents: 카테고리별 {label, count, headings, sample_rules}
        - updates: 변경 로그 최근 N줄
        """
        data = self._load_existing()
        if not data:
            return {
                "ok": True, "synced": False,
                "message": "아직 노션을 동기화하지 않았습니다. 수동 업데이트를 눌러주세요.",
                "agents": {}, "updates": self._tail_log(log_tail),
            }
        cats = data.get("categories") or {}
        agents = {
            key: {
                "label": c.get("label", ""),
                "agent": c.get("agent", ""),
                "count": c.get("count", 0),
                "headings": c.get("headings", [])[:6],
                "sample_rules": [r.get("text", "") for r in c.get("rules", [])[:5]],
            }
            for key, c in cats.items()
        }
        return {
            "ok": True, "synced": True,
            "title": data.get("title", ""),
            "page_id": data.get("source_page_id", ""),
            "last_update": data.get("fetched_at", ""),
            "last_checked": data.get("last_checked", data.get("fetched_at", "")),
            "content_hash": data.get("content_hash", ""),
            "total_rules": (data.get("stats") or {}).get("total_rules", 0),
            "agents": agents,
            "updates": self._tail_log(log_tail),
            # §23 요구 4: 전략 반영 현황(상담 탭 노션 패널).
            "strategy_applied_at": data.get("strategy_applied_at", ""),
            "applied_rules": data.get("applied_rules", []),     # 반영된 규칙 목록
            "aligned_rules": data.get("aligned_rules", []),     # 이미 정합(동일값)
            "pending_rules": data.get("pending_rules", []),     # 미반영(R/R·VWAP 등 도입 예정)
            "conflicts": data.get("conflicts", []),             # 전략 충돌 알림
        }

    # ─────────────────────────── 내부 ───────────────────────────

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _load_existing(self) -> dict | None:
        p = self.knowledge_path
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _atomic_write(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, path)

    def _append_log(self, line: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(f"{self._clock()} | {line}\n")

    def _write_update_log(
        self, prev_hash: str | None, new_hash: str,
        stats: dict, now: str, title: str,
    ) -> None:
        kind = "INITIAL" if prev_hash is None else "CHANGED"
        hash_part = (
            f"{(prev_hash or '------')[:6]}→{new_hash[:6]}"
        )
        per = (f"스크리닝 {stats['screening']}, 신호 {stats['signal']}, "
               f"리스크 {stats['risk']}, 시장 {stats['market_watch']}, "
               f"CEO {stats['ceo']}")
        self._append_log(
            f"{kind} [{hash_part}] '{title}' 총 {stats['total_rules']}건 ({per})"
        )

    def _tail_log(self, n: int) -> list[str]:
        p = self.log_path
        if not p.exists():
            return []
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return lines[-n:]
