"""Paper(모의) 모드 진입점.

CLAUDE.md §17 개정에 따라 paper 모드는 **랜덤 과거 날짜 백테스트 리플레이**로
완전히 대체되었다. 실시간 시세 수신 대신 2023년 이후 랜덤 거래일을 그날인 것처럼
재생한다. 본 파일의 ``main()``은 ``scripts/run_backtest.py``로 위임한다.

``_run()``은 실시간 에이전트 루프이며 ``scripts/run_live.py``(실전 모드)에서 재사용된다.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

# ── 콘솔/파이프 인코딩 고정(요구 1) ──
# 한글 로그가 cp949 콘솔에서 인코딩 예외로 *통째로 사라지는* 것을 막는다. 부모
# 프로세스가 stderr 를 cp949 로 받아도 메시지가 깨질지언정 사라지지는 않는다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — reconfigure 미지원 환경은 무시
        pass


def _write_crash_file(name: str, stage: str, text: str) -> None:
    """콘솔이 닫히거나 부모가 stderr 를 삼켜도 원인이 남도록 영구 파일에 기록."""
    try:
        p = Path(__file__).parents[1] / "state" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            f"[{datetime.now().isoformat()}] stage={stage}\n{text}\n",
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 — 크래시 기록 실패는 비치명
        pass


def _crash_dump(stage: str, exc: BaseException) -> None:
    """예외의 **진짜 원인**(타입·메시지·파일·줄번호 포함 전체 스택)을 절대 삼키지 않는다(요구 1).

    logging 이 아직 설정되지 않은 단계(import 등)에서도 보이도록 stderr 로 **직접**
    출력하고, state/run_paper_last_error.txt 에 영구 기록한다.
    """
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    bar = "=" * 70
    body = (
        f"\n{bar}\n❌ run_paper [{stage}] 예외 — 비정상 종료(코드 1)\n"
        f"   원인: {type(exc).__name__}: {exc}\n{bar}\n{tb}{bar}\n"
    )
    try:
        sys.stderr.write(body)
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    _write_crash_file("run_paper_last_error.txt", stage, tb)


def _excepthook(exc_type, exc, tb) -> None:
    """import 단계 등 **어디서든** 잡히지 않은 예외를 큰 소리로 보고(요구 1·2).

    Ctrl+C(KeyboardInterrupt)는 정상 중단이므로 기본 동작에 맡긴다.
    """
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    _crash_dump("uncaught", exc if exc is not None else exc_type())


sys.excepthook = _excepthook
# 보조 스레드(예: httpx 백그라운드)에서 죽은 예외도 삼켜지지 않게.
threading.excepthook = lambda a: _crash_dump(  # type: ignore[assignment]
    f"thread:{getattr(a.thread, 'name', '?')}",
    a.exc_value or RuntimeError("thread error (no value)"),
)

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
from agents.execution.position_manager.exit_rules import ExitParams
from agents.execution.position_manager.main import PositionManagerAgent
from agents.execution.selector import EntrySelector
from agents.intel.screening.main import (
    ScreeningAgent,
    ScreeningParams,
)
from agents.learning.journal.main import JournalAgent
from agents.learning.notion_sync import NotionSyncAgent
from agents.learning.pattern.main import PatternAnalysisAgent
from agents.meta.optimizer.main import OptimizerAgent
from agents.risk.risk_manager.hard_limits import (
    HardLimitGate,
    HardLimitsConfig,
    StopLossTracker,
)
from agents.risk.risk_manager.main import TOPIC_APPROVED, RiskAgent
from core.kis_client import (
    CreditLedger,
    KisClient,
    KisClientConfig,
    Mode,
    PaperBroker,
)
from core.messaging import Bus
from core.notion_client import (
    NotionAuthError,
    NotionConfig,
    NotionError,
    NotionKnowledgeView,
)

log = logging.getLogger(__name__)


async def _sync_notion_best_effort(root: Path, bus: Bus) -> None:
    """세션 시작 시 학습부가 노션 페이지 최신본을 1회 동기화(비치명).

    토큰 미설정/네트워크 실패는 무시하고 기존 notion_knowledge.json을 그대로 쓴다.
    """
    try:
        cfg = NotionConfig.from_files(project_root=root)
    except NotionAuthError:
        log.info("노션 토큰 미설정 — 동기화 건너뜀(기존 지식 사용)")
        return
    agent = NotionSyncAgent(cfg, memory_dir=root / "data" / "memory", bus=bus)
    try:
        result = await agent.sync()
        if result.get("ok") and result.get("changed"):
            log.info("학습부 노션 동기화: 변경 반영됨")
    except NotionError as exc:
        log.warning("노션 동기화 실패(기존 지식 사용): %s", exc)


def main() -> int:
    # paper 모드 = 랜덤 과거 날짜 백테스트 리플레이 (§17).
    # 백테스트 main()이 모든 예외를 자체적으로 처리한다(전체 스택 트레이스 출력 + 자동
    # 재시작, 종료는 Ctrl+C만 — scripts/run_backtest.py:main). 여기서는 위임만 하되,
    # 위임 자체(임포트 등)에서 나는 예외도 '비정상 종료'로 끝나지 않게 한번 더 감싼다.
    try:
        from scripts.run_backtest import main as backtest_main
        return backtest_main()
    except KeyboardInterrupt:
        log.info("⏹ 사용자 중단(Ctrl+C) — 정상 종료합니다.")
        return 0
    except SystemExit as exc:
        # 하위(backtest)가 명시적으로 정한 종료 코드를 존중하되, 0 이 아니면 원인을 남긴다.
        code = exc.code
        if code is None or code == 0 or code == "":
            return 0
        icode = code if isinstance(code, int) else 1
        _crash_dump("backtest-exit", exc)
        return icode
    except BaseException as exc:  # noqa: BLE001 — 위임(임포트 포함) 단계의 모든 예외
        _crash_dump("delegate", exc)
        return 1


async def _run(expected_mode: Mode) -> int:
    """실시간 에이전트 루프 (live 모드 전용 — run_live.py가 재사용).

    paper 모드는 더 이상 이 경로를 쓰지 않는다(§17 백테스트로 대체).
    """
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

    # 학습부: 세션 시작 시 노션 페이지 동기화(비치명) → notion_knowledge.json 갱신.
    await _sync_notion_best_effort(root, bus)
    # 각 에이전트가 참조할 노션 지식 뷰(세션 시작 시 1회 로드).
    notion = NotionKnowledgeView.load(root)

    # 모의 모드: 시세는 실전 키로 실제 수신, 주문만 가상 체결(§3.1).
    paper_broker = None
    if cfg.simulate_orders:
        paper_broker = PaperBroker(
            persist_path=root / "state" / "paper_broker.json",
            start_cash=cfg.paper_start_cash,
        )
        log.info(
            "PAPER 모드: 주문 가상 시뮬레이션 (시작 가상자금 %s원), 시세는 실전 데이터",
            f"{cfg.paper_start_cash:,}",
        )

    async with KisClient(cfg, credit_ledger=ledger, paper_broker=paper_broker) as kis:
        ceo = CeoAgent(kis, bus, project_root=root, notion_knowledge=notion)
        await ceo.bootstrap()

        hl_cfg = HardLimitsConfig.from_file(root / "config" / "hard_limits.yaml")
        sig_params = SignalParams.from_file(root / "config" / "strategy_params.yaml")
        scr_params = ScreeningParams.from_file(root / "config" / "strategy_params.yaml")
        exit_params = ExitParams.from_file(root / "config" / "strategy_params.yaml")

        # HL-02 연속손절 카운터는 진입 게이트와 청산 매니저가 공유한다.
        tracker = StopLossTracker()

        analyzer = SignalAnalyzer(sig_params)
        market_watch = MarketWatchAgent(kis, bus, notion_knowledge=notion)
        screening = ScreeningAgent(kis, bus, scr_params, notion_knowledge=notion)
        signal_agent = SignalAgent(kis, analyzer, bus, notion_knowledge=notion)
        selector = EntrySelector()

        current_grade = {"v": MarketGrade.GREEN}

        async def on_state(state: MarketState) -> None:
            current_grade["v"] = state.grade
            if state.grade == MarketGrade.BLACK:
                ceo.kill(reason=f"market BLACK ({state.reason})")

        bus.subscribe(TOPIC_STATE, on_state)

        risk = RiskAgent(
            kis, HardLimitGate(hl_cfg, stoploss_tracker=tracker), bus,
            market_state_provider=lambda: current_grade["v"],
            notion_knowledge=notion,
        )
        order = OrderAgent(kis, bus)
        pos_mgr = PositionManagerAgent(kis, bus, order, analyzer, exit_params, tracker)

        async def on_entry(entry) -> None:
            await risk.review(entry)

        async def on_approved(approved) -> None:
            await order.execute(approved)

        bus.subscribe(TOPIC_ENTRY, on_entry)
        bus.subscribe(TOPIC_APPROVED, on_approved)

        # paper-only 학습부 활성화는 instance 생성으로 충분 (run_daily는 외부 트리거)
        PatternAnalysisAgent(cfg.mode, bus, root / "data" / "journal")

        # 메타부 — 관찰은 양 모드, 제안은 paper 전용. 제안은 CEO 승인 후에만 적용(§2.7/§11).
        optimizer = OptimizerAgent(
            cfg.mode, bus, root / "data" / "journal",
            config_path=root / "config" / "strategy_params.yaml",
        )

        async def maybe_enter(candidates: list) -> None:
            """무보유일 때만 그 시점 최강 1종목 진입 (§5.7 단일 집중)."""
            if not pos_mgr.is_flat():
                return
            try:
                balance = await kis.get_balance()
            except Exception:
                log.exception("maybe_enter: balance fetch failed")
                return
            if not selector.is_flat(balance):
                return
            best = selector.pick(candidates)
            if best is None:
                return
            await signal_agent.analyze_symbol(best.code)  # → entry→risk→order

        # 백그라운드 루프 — 한 에이전트가 죽어도 **전체는 계속**(요구 4). 각 루프를
        # 개별 try/except 로 감싸 예외를 큰 소리로 남기되 다른 태스크/프로세스를
        # 끌어내리지 않는다(정지는 Ctrl+C/Kill 로만).
        async def market_loop() -> None:
            try:
                await market_watch.run_forever(ceo.stop_event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("market_watch 루프 비정상 종료 — 격리(전체는 계속)")

        async def screening_loop() -> None:
            while not ceo.stop_event.is_set():
                try:
                    candidates = await screening.screen_once()
                    await maybe_enter(candidates)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("screening failed")
                try:
                    await asyncio.wait_for(ceo.stop_event.wait(), timeout=300)
                except asyncio.TimeoutError:
                    continue

        async def position_loop() -> None:
            try:
                await pos_mgr.run_forever(ceo.stop_event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("position_manager 루프 비정상 종료 — 격리(전체는 계속)")

        async def optimizer_loop() -> None:
            try:
                await optimizer.run_forever(ceo.stop_event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("meta_optimizer 루프 비정상 종료 — 격리(전체는 계속)")

        tasks = [
            asyncio.create_task(market_loop(), name="market_watch"),
            asyncio.create_task(screening_loop(), name="screening"),
            asyncio.create_task(position_loop(), name="position_manager"),
            asyncio.create_task(optimizer_loop(), name="meta_optimizer"),
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
    try:
        _code = main()
    except KeyboardInterrupt:
        log.info("⏹ 사용자 중단(Ctrl+C) — 정상 종료합니다.")
        _code = 0
    except BaseException as _e:  # noqa: BLE001 — 최상위 안전망: 절대 조용히 죽지 않는다
        _crash_dump("toplevel", _e)
        _code = 1
    sys.exit(_code)
