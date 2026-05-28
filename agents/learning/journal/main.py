"""Journal agent — always-on append-only logger (CLAUDE.md §2.6, §11).

모든 토픽을 구독하여 ``data/journal/{YYYYMMDD}.jsonl``에 append.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from agents.analysis.signal.indicators import KST
from core.messaging import Bus

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


def _serialize(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _serialize(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


class JournalAgent:
    def __init__(
        self,
        bus: Bus,
        journal_dir: Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(KST),
        topics: tuple[str, ...] = SUBSCRIBED_TOPICS,
    ) -> None:
        self._bus = bus
        self._dir = journal_dir
        self._clock = clock
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
        record = {
            "ts": now.isoformat(),
            "topic": topic,
            "payload": _serialize(payload),
        }
        async with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
