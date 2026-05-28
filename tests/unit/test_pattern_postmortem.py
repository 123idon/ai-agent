"""Unit tests for learning/pattern and learning/postmortem."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from agents.learning.pattern.main import PaperModeRequired, PatternAnalysisAgent
from agents.learning.postmortem.main import (
    LiveModeRequired,
    PostmortemPackager,
)
from core.kis_client import Mode
from core.messaging import Bus


def _write_journal(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


async def test_pattern_summarizes_daily_journal(tmp_path: Path) -> None:
    _write_journal(tmp_path / "20260529.jsonl", [
        {"ts": "2026-05-29T10:00:00", "topic": "signal.entry", "payload": {"x": 1}},
        {"ts": "2026-05-29T10:01:00", "topic": "signal.entry", "payload": {"x": 2}},
        {"ts": "2026-05-29T10:02:00", "topic": "risk.decision.approved", "payload": {}},
        {"ts": "2026-05-29T10:03:00", "topic": "order.event", "payload": {}},
        {"ts": "2026-05-29T10:04:00", "topic": "risk.decision.rejected", "payload": {}},
    ])
    bus = Bus()
    agent = PatternAnalysisAgent(Mode.PAPER, bus, tmp_path)
    summary = await agent.run_daily("20260529")
    assert summary.record_count == 5
    assert summary.approved_count == 1
    assert summary.rejected_count == 1
    assert summary.order_event_count == 1
    assert summary.topic_counts["signal.entry"] == 2


async def test_pattern_blocks_live_mode(tmp_path: Path) -> None:
    with pytest.raises(PaperModeRequired):
        PatternAnalysisAgent(Mode.LIVE, Bus(), tmp_path)


def test_postmortem_packages_zip(tmp_path: Path) -> None:
    journal_dir = tmp_path / "journal"
    output_dir = tmp_path / "out"
    _write_journal(journal_dir / "20260529.jsonl", [
        {"ts": "2026-05-29T10:00:00", "topic": "order.event", "payload": {"ord_no": "1"}},
    ])
    pkg = PostmortemPackager(Mode.LIVE, journal_dir, output_dir)
    artifact = pkg.package_day("20260529")
    assert artifact.zip_path.exists()
    assert artifact.record_count == 1
    with zipfile.ZipFile(artifact.zip_path) as zf:
        names = zf.namelist()
        assert "journal_20260529.jsonl" in names
        assert "SUMMARY.md" in names


def test_postmortem_blocks_paper_mode(tmp_path: Path) -> None:
    with pytest.raises(LiveModeRequired):
        PostmortemPackager(Mode.PAPER, tmp_path, tmp_path)


def test_postmortem_handles_missing_journal(tmp_path: Path) -> None:
    pkg = PostmortemPackager(Mode.LIVE, tmp_path / "journal", tmp_path / "out")
    artifact = pkg.package_day("20991231")
    assert artifact.zip_path.exists()
    assert artifact.record_count == 0
