"""CEO agent (CLAUDE.md §2.1, §10).

부트스트랩 / 모드 잠금 해시 캡처 / Kill Switch 센티넬 폴링.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from agents.meta.optimizer.main import apply_proposal_to_file
from core.kis_client import KisClient, Mode
from core.messaging import Bus
from core.notion_client import NotionKnowledgeView

log = logging.getLogger(__name__)

TOPIC_COMMAND = "ceo.command"
TOPIC_PROPOSAL = "learning.proposal"
KILL_SENTINEL = "KILL_SWITCH"


class StrategyLockBroken(RuntimeError):
    """live 모드에서 strategy_params.yaml 해시가 변경되었을 때 발생."""


class CeoAgent:
    def __init__(
        self,
        kis: KisClient,
        bus: Bus,
        *,
        project_root: Path,
        notion_knowledge: NotionKnowledgeView | None = None,
    ) -> None:
        self._kis = kis
        self._bus = bus
        self._root = project_root
        self._stop = asyncio.Event()
        self._strategy_params_hash: str | None = None
        self._sentinel_path = self._root / "state" / KILL_SENTINEL
        # 메타부 제안 승인 큐 (§2.7 / §11) — 자동 적용 금지, CEO 승인 후에만 반영
        self._pending_proposals: dict[str, Any] = {}
        self._bus.subscribe(TOPIC_PROPOSAL, self._on_proposal)
        # 학습부 노션 지식(세션 시작 시 참조) — 기타 매매 원칙(심리·복기·규칙) 카테고리.
        self._notion = notion_knowledge
        if notion_knowledge is not None and notion_knowledge.available:
            note = notion_knowledge.summary_line("ceo")
            if note:
                log.info("📚 CEO %s", note)

    @property
    def stop_event(self) -> asyncio.Event:
        return self._stop

    # ─────────────────────────── 제안 승인 (§2.7, §11) ───────────────────────────

    @property
    def pending_proposals(self) -> dict[str, Any]:
        return dict(self._pending_proposals)

    async def _on_proposal(self, payload: Any) -> None:
        """``learning.proposal`` 수신. proposal_id가 있는 메타부 제안만 큐에 적재.

        (PatternAnalysisAgent의 DailySummary 등 비-제안 페이로드는 무시한다.)
        """
        pid = getattr(payload, "proposal_id", None)
        if pid is None:
            return
        self._pending_proposals[pid] = payload
        log.info(
            "PROPOSAL_PENDING %s kind=%s — 운영자 승인 대기",
            pid, getattr(payload, "kind", "?"),
        )

    def approve_proposal(self, proposal_id: str) -> bool:
        """운영자 승인 → 제안 적용. **paper 모드 + 잠금 정상**일 때만 파라미터 반영.

        - live 모드: 전략 파라미터 잠금(§3.1/§3.3)으로 적용 거부.
        - token 제안: 파라미터 변경 없음 → 권고만 승인 기록.
        """
        proposal = self._pending_proposals.get(proposal_id)
        if proposal is None:
            log.warning("approve_proposal: unknown id %s", proposal_id)
            return False

        if self._kis.mode != Mode.PAPER:
            log.warning(
                "approve_proposal %s 거부: live 모드는 파라미터 잠금(§3.3)", proposal_id,
            )
            return False

        changes = tuple(getattr(proposal, "changes", ()) or ())
        if not changes:
            # 파라미터 변경이 없는 제안(토큰 권고 등)은 승인 기록만
            self._pending_proposals.pop(proposal_id, None)
            log.info("PROPOSAL_APPROVED %s (권고, 파라미터 변경 없음)", proposal_id)
            return True

        applied = apply_proposal_to_file(
            proposal, self._root / "config" / "strategy_params.yaml",
        )
        self._pending_proposals.pop(proposal_id, None)
        if not applied:
            log.warning("PROPOSAL_APPROVED %s 이나 적용된 변경 없음", proposal_id)
            return False
        log.info(
            "PROPOSAL_APPLIED %s: %s",
            proposal_id,
            ", ".join(f"{c.key}:{c.from_value}->{c.to_value}" for c in applied),
        )
        return True

    def reject_proposal(self, proposal_id: str) -> bool:
        if self._pending_proposals.pop(proposal_id, None) is None:
            return False
        log.info("PROPOSAL_REJECTED %s", proposal_id)
        return True

    def load_session_memory(self) -> str:
        """세션 시작 시 영구 기억(개선사항·상담 맥락·노션 반영)을 떠올려 로그로 남긴다.

        에이전트가 매 세션 기억을 초기화하던 문제(§11/§19/§23)를 해결한다. 순수
        파일 읽기라 부작용이 없고, 기억이 없으면 빈 문자열을 돌려준다.
        """
        try:
            from core.memory import session_learning_brief
            brief = session_learning_brief(self._root / "data" / "memory")
        except Exception:  # noqa: BLE001 — 기억 로드 실패가 부팅을 막지 않도록
            log.debug("session memory load failed", exc_info=True)
            return ""
        if brief:
            for line in brief.splitlines():
                log.info("🧠 %s", line)
        return brief

    async def bootstrap(self) -> None:
        token = await self._kis.fetch_token()
        log.info("KIS token issued: %s", token.token)
        # 영구 기억 로드(세션 간 기억 유지, §11/§19/§23).
        self.load_session_memory()
        balance = await self._kis.get_balance()
        log.info(
            "Start balance: cash=%s, totalEval=%s, positions=%d",
            balance.cash, balance.totalEval, len(balance.positions),
        )
        if self._kis.mode == Mode.LIVE:
            self._strategy_params_hash = self._compute_strategy_hash()
            log.info("LIVE: strategy_params hash captured = %s",
                     self._strategy_params_hash[:16])
        # 부팅 시 이전 세션의 KILL 센티넬은 정리
        if self._sentinel_path.exists():
            self._sentinel_path.unlink()

    def _compute_strategy_hash(self) -> str:
        path = self._root / "config" / "strategy_params.yaml"
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def verify_strategy_lock(self) -> bool:
        """live 모드만 호출. 해시 불일치 시 False."""
        if self._kis.mode != Mode.LIVE:
            return True
        if self._strategy_params_hash is None:
            return False
        return self._compute_strategy_hash() == self._strategy_params_hash

    def kill(self, reason: str = "manual") -> None:
        log.warning("KILL SWITCH activated: %s", reason)
        self._stop.set()

    async def run_forever(self, *, poll_interval: float = 1.0) -> None:
        log.info("CEO running. Mode=%s", self._kis.mode.value)
        while not self._stop.is_set():
            if self._sentinel_path.exists():
                self.kill(reason="sentinel file detected")
                break
            if self._kis.mode == Mode.LIVE and not self.verify_strategy_lock():
                self.kill(reason="strategy_params.yaml hash mismatch")
                raise StrategyLockBroken("strategy_params.yaml hash mismatch")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                continue
        log.info("CEO stopped")
