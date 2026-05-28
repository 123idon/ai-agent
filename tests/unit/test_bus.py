"""Tests for the in-process Bus."""
from __future__ import annotations

from core.messaging import Bus


async def test_publish_with_no_subscribers_is_noop() -> None:
    bus = Bus()
    await bus.publish("topic.x", {"v": 1})  # 예외 발생하지 않으면 통과


async def test_multiple_subscribers_all_receive() -> None:
    bus = Bus()
    a = bus.collector("topic.x")
    b = bus.collector("topic.x")
    await bus.publish("topic.x", "hello")
    assert a == ["hello"]
    assert b == ["hello"]


async def test_subscriber_failure_isolated() -> None:
    bus = Bus()
    received: list[str] = []

    async def failing(_payload: object) -> None:
        raise RuntimeError("boom")

    async def good(payload: object) -> None:
        received.append(str(payload))

    bus.subscribe("topic.x", failing)
    bus.subscribe("topic.x", good)
    await bus.publish("topic.x", "ok")
    assert received == ["ok"]
