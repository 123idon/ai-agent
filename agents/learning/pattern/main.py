"""Pattern / backtest agent — PAPER MODE ONLY (CLAUDE.md §2.6, §11).

paper 모드에서 일일 journal을 읽어 토픽별 통계를 산출하고
``learning.proposal`` 토픽으로 CEO에게 제안한다. 자동 파라미터 변경 권한 없음.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from core.kis_client import Mode
from core.messaging import Bus

log = logging.getLogger(__name__)

TOPIC_PROPOSAL = "learning.proposal"


class PaperModeRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class DailySummary:
    date: str
    record_count: int
    topic_counts: dict[str, int]
    approved_count: int
    rejected_count: int
    order_event_count: int
    order_failed_count: int


class PatternAnalysisAgent:
    """가벼운 v1: 일일 토픽별 카운트만 산출. 백테스트는 후속 작업."""

    def __init__(self, mode: Mode, bus: Bus, journal_dir: Path) -> None:
        if mode != Mode.PAPER:
            raise PaperModeRequired("pattern analysis is paper-only (§11)")
        self._mode = mode
        self._bus = bus
        self._dir = journal_dir

    async def run_daily(self, date: str) -> DailySummary:
        path = self._dir / f"{date}.jsonl"
        if not path.exists():
            summary = DailySummary(
                date=date, record_count=0, topic_counts={},
                approved_count=0, rejected_count=0,
                order_event_count=0, order_failed_count=0,
            )
        else:
            counts: Counter[str] = Counter()
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    counts[rec.get("topic", "")] += 1
            summary = DailySummary(
                date=date,
                record_count=sum(counts.values()),
                topic_counts=dict(counts),
                approved_count=counts.get("risk.decision.approved", 0),
                rejected_count=counts.get("risk.decision.rejected", 0),
                order_event_count=counts.get("order.event", 0),
                order_failed_count=counts.get("order.failed", 0),
            )
        log.info("pattern daily summary %s: %d records", date, summary.record_count)
        await self._bus.publish(TOPIC_PROPOSAL, summary)
        return summary
