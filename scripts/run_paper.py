"""Boot all agents in paper mode (CLAUDE.md §9)."""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from agents.analysis.signal.indicators import SignalAnalyzer, SignalParams
from agents.analysis.signal.main import TOPIC_ENTRY, SignalAgent
from agents.ceo.main import CeoAgent
from agents.execution.order.main import OrderAgent
from agents.intel.market_watch.main import (
    TOPIC_STATE,
    MarketGrade,
    MarketState,
    MarketWatchAgent,
)
from agents.intel.screening.main import (
    TOPIC_CANDIDATES,
    ScreeningAgent,
    ScreeningParams,
)
from agents.learning.journal.main import JournalAgent
from agents.learning.pattern.main import PatternAnalysisAgent
from agents.risk.risk_manager.hard_limits import HardLimitGate, HardLimitsConfig
from agents.risk.risk_manager.main import TOPIC_APPROVED, RiskAgent
from core.kis_client import CreditLedger, KisClient, KisClientConfig, Mode
from core.messaging import Bus

log = logging.getLogger(__name__)


def main() -> int:
    return asyncio.run(_run(Mode.PAPER))


async def _run(expected_mode: Mode) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    root = Path(__file__).parents[1]
    cfg = KisClientConfig.from_files(project_root=root)
    if cfg.mode != expected_mode:
        log.error("mode mismatch: config=%s, expected=%s",
                  cfg.mode.value, expected_mode.value)
        return 1

    bus = Bus()
    JournalAgent(bus, root / "data" / "journal")
    ledger = CreditLedger(root / "state" / "credit_ledger.json")

    async with KisClient(cfg, credit_ledger=ledger) as kis:
        ceo = CeoAgent(kis, bus, project_root=root)
        await ceo.bootstrap()

        hl_cfg = HardLimitsConfig.from_file(root / "config" / "hard_limits.yaml")
        sig_params = SignalParams.from_file(root / "config" / "strategy_params.yaml")
        scr_params = ScreeningParams.from_file(root / "config" / "strategy_params.yaml")

        market_watch = MarketWatchAgent(kis, bus)
        screening = ScreeningAgent(kis, bus, scr_params)
        signal_agent = SignalAgent(kis, SignalAnalyzer(sig_params), bus)

        current_grade = {"v": MarketGrade.GREEN}

        async def on_state(state: MarketState) -> None:
            current_grade["v"] = state.grade
            if state.grade == MarketGrade.BLACK:
                ceo.kill(reason=f"market BLACK ({state.reason})")

        bus.subscribe(TOPIC_STATE, on_state)

        risk = RiskAgent(
            kis, HardLimitGate(hl_cfg), bus,
            market_state_provider=lambda: current_grade["v"],
        )
        order = OrderAgent(kis, bus)

        async def on_candidate(cand) -> None:
            await signal_agent.analyze_symbol(cand.code)

        async def on_entry(entry) -> None:
            await risk.review(entry)

        async def on_approved(approved) -> None:
            await order.execute(approved)

        bus.subscribe(TOPIC_CANDIDATES, on_candidate)
        bus.subscribe(TOPIC_ENTRY, on_entry)
        bus.subscribe(TOPIC_APPROVED, on_approved)

        # paper-only 학습부 활성화는 instance 생성으로 충분 (run_daily는 외부 트리거)
        PatternAnalysisAgent(cfg.mode, bus, root / "data" / "journal")

        # 백그라운드 루프
        async def market_loop() -> None:
            await market_watch.run_forever(ceo.stop_event)

        async def screening_loop() -> None:
            while not ceo.stop_event.is_set():
                try:
                    await screening.screen_once()
                except Exception:
                    log.exception("screening failed")
                try:
                    await asyncio.wait_for(ceo.stop_event.wait(), timeout=300)
                except asyncio.TimeoutError:
                    continue

        tasks = [
            asyncio.create_task(market_loop(), name="market_watch"),
            asyncio.create_task(screening_loop(), name="screening"),
        ]
        try:
            await ceo.run_forever()
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
