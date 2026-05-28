"""In-process async pub/sub bus for inter-agent messaging.

추후 redis-streams (CLAUDE.md §6.1)로 교체할 때 본 ``Bus`` 인터페이스만
동일하게 유지하면 호출부 변경 없이 swap-in 가능하다.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

Handler = Callable[[Any], Awaitable[None]]


class Bus:
    """Topic-based async pub/sub. 구독자 간 격리 (한 핸들러 실패가 다른 핸들러를 막지 않음)."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subscribers[topic].append(handler)

    async def publish(self, topic: str, payload: Any) -> None:
        handlers = list(self._subscribers.get(topic, []))
        if not handlers:
            return
        await asyncio.gather(
            *(self._safe_call(h, topic, payload) for h in handlers),
            return_exceptions=False,
        )

    async def _safe_call(self, handler: Handler, topic: str, payload: Any) -> None:
        try:
            await handler(payload)
        except Exception:
            log.exception("subscriber error on topic=%s", topic)

    def collector(self, topic: str) -> list[Any]:
        """테스트용 — 해당 토픽의 모든 publish 페이로드를 리스트에 모은다."""
        bucket: list[Any] = []

        async def _collect(payload: Any) -> None:
            bucket.append(payload)

        self.subscribe(topic, _collect)
        return bucket


__all__ = ["Bus", "Handler"]
