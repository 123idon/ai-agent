"""Tests for core.schemas envelope (CLAUDE.md §6.2)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from core.schemas import KST, Envelope, Topic, parse, wrap


class Mood(str, Enum):
    OK = "ok"


@dataclass(frozen=True)
class Sample:
    symbol: str
    qty: int
    mood: Mood
    when: datetime


def test_wrap_creates_iso_envelope() -> None:
    sample = Sample(symbol="005930", qty=10, mood=Mood.OK,
                    when=datetime(2026, 5, 29, 10, 30, tzinfo=KST))
    env = wrap("signal.entry", sample, sender="bus", mode="paper")

    assert env["topic"] == "signal.entry"
    assert env["mode"] == "paper"
    assert env["sender"] == "bus"
    assert env["payload"]["symbol"] == "005930"
    assert env["payload"]["qty"] == 10
    assert env["payload"]["mood"] == "ok"
    assert env["payload"]["when"].startswith("2026-05-29T10:30")
    assert "msg_id" in env and "trace_id" in env
    assert env["ts"]


def test_parse_roundtrip() -> None:
    env_dict = wrap(
        "order.event", {"x": 1}, sender="exec", mode="live",
        ts=datetime(2026, 5, 29, 10, 30, tzinfo=KST),
        msg_id="m1", trace_id="t1",
    )
    parsed = parse(env_dict)
    assert isinstance(parsed, Envelope)
    assert parsed.topic == "order.event"
    assert parsed.mode == "live"
    assert parsed.payload == {"x": 1}
    assert parsed.msg_id == "m1"
    assert parsed.trace_id == "t1"


def test_topic_enum_matches_strings() -> None:
    assert Topic.SCREENING_CANDIDATES.value == "screening.candidates"
    assert Topic.RISK_APPROVED.value == "risk.decision.approved"
    assert Topic.ORDER_EVENT.value == "order.event"


def test_journal_writes_envelope_format(tmp_path) -> None:
    """JournalAgent가 envelope으로 직렬화하는지 확인."""
    import asyncio
    import json
    from dataclasses import dataclass

    from agents.learning.journal.main import JournalAgent
    from core.kis_client import Mode
    from core.messaging import Bus

    @dataclass(frozen=True)
    class P:
        x: int

    bus = Bus()
    fixed = datetime(2026, 5, 29, 10, 30, tzinfo=KST)
    JournalAgent(bus, tmp_path, mode=Mode.PAPER, clock=lambda: fixed)
    asyncio.run(bus.publish("signal.entry", P(x=42)))

    rec = json.loads((tmp_path / "20260529.jsonl").read_text("utf-8").splitlines()[0])
    assert rec["topic"] == "signal.entry"
    assert rec["mode"] == "paper"
    assert rec["sender"] == "bus"
    assert rec["payload"]["x"] == 42
    assert "msg_id" in rec and "trace_id" in rec
