"""Journal agent — always-on append-only logger (CLAUDE.md §2.6, §11).

모든 토픽을 구독하여 ``data/journal/{YYYYMMDD}.jsonl``에 envelope 포맷으로 append.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from agents.analysis.signal.indicators import KST
from core.kis_client import Mode
from core.messaging import Bus
from core.schemas import wrap

log = logging.getLogger(__name__)


SUBSCRIBED_TOPICS = (
    "signal.entry",
    "risk.decision.approved",
    "risk.decision.rejected",
    "order.event",
    "order.failed",
    "market.state",
    "screening.candidates",
)


class JournalAgent:
    def __init__(
        self,
        bus: Bus,
        journal_dir: Path,
        *,
        mode: Mode | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(KST),
        topics: tuple[str, ...] = SUBSCRIBED_TOPICS,
        sender: str = "bus",
    ) -> None:
        self._bus = bus
        self._dir = journal_dir
        self._mode = mode.value if mode else "unknown"
        self._clock = clock
        self._sender = sender
        self._lock = asyncio.Lock()
        for topic in topics:
            self._bus.subscribe(topic, self._make_handler(topic))

    def _make_handler(self, topic: str) -> Callable[[Any], Any]:
        async def _handler(payload: Any) -> None:
            await self._write(topic, payload)
        return _handler

    async def _write(self, topic: str, payload: Any) -> None:
        now = self._clock()
        path = self._dir / f"{now.strftime('%Y%m%d')}.jsonl"
        envelope = wrap(
            topic, payload,
            sender=self._sender, mode=self._mode, ts=now,
        )
        async with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(envelope, ensure_ascii=False, default=str) + "\n")
