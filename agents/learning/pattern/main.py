"""Pattern / backtest agent — PAPER MODE ONLY (CLAUDE.md §2.6, §11)."""
from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agents.analysis.signal.indicators import (
    Direction,
    SignalAnalyzer,
)
from agents.learning.pattern.backtest import (
    BacktestEngine,
    BacktestParams,
    BacktestResult,
)
from core.indicators import CandleLike
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
    """일일 journal 요약 + 백테스트 실행 + 제안 발행 (paper 전용)."""

    def __init__(
        self,
        mode: Mode,
        bus: Bus,
        journal_dir: Path,
        *,
        analyzer: SignalAnalyzer | None = None,
        backtest_params: BacktestParams | None = None,
    ) -> None:
        if mode != Mode.PAPER:
            raise PaperModeRequired("pattern analysis is paper-only (§11)")
        self._mode = mode
        self._bus = bus
        self._dir = journal_dir
        self._analyzer = analyzer
        self._backtest_params = backtest_params

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

    def backtest(
        self,
        candles: Sequence[CandleLike],
        *,
        symbol: str = "TEST",
        direction: Direction = Direction.LONG,
    ) -> BacktestResult:
        """주어진 분봉 시퀀스에 대해 walk-forward 백테스트 실행.

        ``analyzer``가 생성자에 주입되지 않은 경우 ``ValueError``.
        """
        if self._analyzer is None:
            raise ValueError("analyzer was not provided to PatternAnalysisAgent")
        engine = BacktestEngine(self._analyzer, self._backtest_params)
        return engine.run(candles, symbol=symbol, direction=direction)
