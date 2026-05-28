"""CEO agent (CLAUDE.md §2.1, §10).

부트스트랩 / 모드 잠금 해시 캡처 / Kill Switch 센티넬 폴링.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from core.kis_client import KisClient, Mode
from core.messaging import Bus

log = logging.getLogger(__name__)

TOPIC_COMMAND = "ceo.command"
KILL_SENTINEL = "KILL_SWITCH"


class StrategyLockBroken(RuntimeError):
    """live 모드에서 strategy_params.yaml 해시가 변경되었을 때 발생."""


class CeoAgent:
    def __init__(self, kis: KisClient, bus: Bus, *, project_root: Path) -> None:
        self._kis = kis
        self._bus = bus
        self._root = project_root
        self._stop = asyncio.Event()
        self._strategy_params_hash: str | None = None
        self._sentinel_path = self._root / "state" / KILL_SENTINEL

    @property
    def stop_event(self) -> asyncio.Event:
        return self._stop

    async def bootstrap(self) -> None:
        token = await self._kis.fetch_token()
        log.info("KIS token issued: %s", token.token)
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
