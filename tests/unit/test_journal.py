"""Unit tests for JournalAgent."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agents.analysis.signal.indicators import KST
from agents.learning.journal.main import SUBSCRIBED_TOPICS, JournalAgent
from core.messaging import Bus


@dataclass(frozen=True)
class FakePayload:
    symbol: str
    qty: int


async def test_journal_writes_jsonl_on_publish(tmp_path: Path) -> None:
    bus = Bus()
    fixed = datetime(2026, 5, 29, 10, 30, tzinfo=KST)
    JournalAgent(bus, tmp_path, clock=lambda: fixed)

    await bus.publish("signal.entry", FakePayload(symbol="005930", qty=10))
    await bus.publish("order.event", FakePayload(symbol="000660", qty=5))

    path = tmp_path / "20260529.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rec0 = json.loads(lines[0])
    assert rec0["topic"] == "signal.entry"
    assert rec0["payload"]["symbol"] == "005930"
    assert rec0["payload"]["qty"] == 10
    assert rec0["ts"].startswith("2026-05-29T10:30")


async def test_journal_subscribes_all_topics(tmp_path: Path) -> None:
    bus = Bus()
    fixed = datetime(2026, 5, 29, 10, 30, tzinfo=KST)
    JournalAgent(bus, tmp_path, clock=lambda: fixed)
    for topic in SUBSCRIBED_TOPICS:
        await bus.publish(topic, FakePayload(symbol="X", qty=1))
    path = tmp_path / "20260529.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(SUBSCRIBED_TOPICS)
