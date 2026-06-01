"""랜덤 과거 날짜 백테스트 리플레이 (CLAUDE.md §17 / paper 모드 대체).

2023년 이후 랜덤 거래일을 골라, 그날 09:00~15:30을 분 단위로 재생하며 전 에이전트를
그 시점의 가상 시각으로 구동한다. 미래 데이터는 ``ReplayKisClient``가 차단한다.
하루가 끝나면 다음 랜덤 거래일로 자동 진행한다.

데이터 소스 (모두 traidair 경유, 무료 — 유료 KRX 제거):
- 지수(코스피/코스닥) : traidair sim ``market-data`` (Yahoo ^KS11/^KQ11 백엔드).
- 종목 데이터(유니버스): KIS ``volume-rank`` (traidair).
- 분봉(5지표·청산)    : KIS ``chart`` (traidair, 룩어헤드 컷오프).
- 공시               : DART (traidair) — 과거 일자 재현은 traidair 날짜 파라미터 필요(§15.5).

환경변수:
- BACKTEST_START : 시작일 YYYY-MM-DD (기본 2023-01-01)
- BACKTEST_END   : 종료일 YYYY-MM-DD (기본 어제)
- BACKTEST_DAYS  : 재생할 거래일 수 (기본 무제한; Ctrl+C로 중단)
- BACKTEST_SEED  : 난수 시드 (재현용)
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import random
import signal
import sys
import threading
import time as _walltime
import traceback
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

# ── 콘솔/파이프 인코딩 고정(요구 1) ──
# cp949 콘솔에서 한글 로그가 인코딩 예외로 통째로 사라지는 것을 막는다(메시지가
# 깨질지언정 절대 사라지지 않게 — errors='backslashreplace').
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass


def _write_crash_file(stage: str, text: str) -> None:
    """콘솔이 닫히거나 부모가 stderr 를 삼켜도 원인이 남도록 영구 파일에 기록(요구 1)."""
    try:
        p = Path(__file__).parents[1] / "state" / "run_backtest_last_error.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            f"[{datetime.now().isoformat()}] stage={stage}\n{text}\n",
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def _crash_dump(stage: str, exc: BaseException) -> None:
    """예외의 **진짜 원인**(타입·메시지·파일·줄번호 포함 전체 스택)을 stderr+파일에 남긴다(요구 1).

    logging 미설정 단계(import 등)에서도 보이도록 stderr 로 직접 출력한다.
    """
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    bar = "=" * 70
    body = (
        f"\n{bar}\n❌ run_backtest [{stage}] 예외 — 정확한 원인 + 전체 스택\n"
        f"   원인: {type(exc).__name__}: {exc}\n{bar}\n{tb}{bar}\n"
    )
    try:
        sys.stderr.write(body)
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    _write_crash_file(stage, tb)


def _excepthook(exc_type, exc, tb) -> None:
    """import 단계 등 **어디서든** 잡히지 않은 예외를 큰 소리로 보고(요구 1).

    Ctrl+C 는 정상 중단이므로 기본 동작에 맡긴다.
    """
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    _crash_dump("uncaught", exc if exc is not None else exc_type())


sys.excepthook = _excepthook
threading.excepthook = lambda a: _crash_dump(  # type: ignore[assignment]
    f"thread:{getattr(a.thread, 'name', '?')}",
    a.exc_value or RuntimeError("thread error (no value)"),
)

from agents.analysis.signal.indicators import SignalAnalyzer, SignalParams
from agents.analysis.signal.main import TOPIC_ENTRY, SignalAgent
from agents.execution.order.main import OrderAgent
from agents.execution.position_manager.exit_rules import ExitParams
from agents.execution.position_manager.main import PositionManagerAgent
from agents.execution.selector import EntrySelector
from agents.intel.market_watch.main import (
    TOPIC_STATE,
    MarketGrade,
    MarketState,
    MarketWatchAgent,
)
from agents.intel.screening.main import ScreeningAgent, ScreeningParams
from agents.learning.journal.main import JournalAgent
from agents.meta.optimizer.main import OptimizerAgent
from agents.risk.risk_manager.hard_limits import (
    HardLimitGate,
    HardLimitsConfig,
    StopLossTracker,
)
from agents.risk.risk_manager.main import TOPIC_APPROVED, RiskAgent
from core.backtest import BacktestDashboard, BacktestRunner, ReplayKisClient
from core.kis_client import KisClient, KisClientConfig, Mode
from core.marketdata import CandleStore, load_universe, name_map
from core.memory import MemoryStore
from core.messaging import Bus
from core.notion_client import NotionKnowledgeView
from core.time_utils import KST, SimClock, at_kst, from_ymd

log = logging.getLogger(__name__)

PROBE_CODE = "005930"   # 삼성전자 — 거래일 여부 확인용 (분봉 존재 = 거래일)


def _env_date(name: str, default: date) -> date:
    v = os.getenv(name)
    if not v:
        return default
    return datetime.strptime(v, "%Y-%m-%d").date()


class AutoSpeedGovernor:
    """시스템 부하 기반 자동 배속 페이서 + 일시정지 게이트 (요구 1/2).

    백테스트는 단일 스레드 async라 기본적으로 한 코어를 전속력으로 쓴다. 이 거버너는
    분 단위 스텝마다 호출되어:
      - **일시정지 게이트**: ``pause_path`` 센티넬이 존재하는 동안 가상 시각을 전진시키지
        않고 대기한다(프로세스는 살아 있어 '이어서 진행'이 가능). 대시보드는 그 사이에도
        계속 기록하므로 HTS는 같은 캔들/시각에서 멈춘 채로 보인다.
      - 매 스텝 ``asyncio.sleep(0)``으로 이벤트 루프에 양보(대시보드 파일 기록 등 보장).
      - ``psutil`` 사용 가능 시 시스템 CPU 사용률을 주기적으로 표본화해, 임계
        (기본 88%)를 넘으면 짧게 sleep을 넣어 머신이 버벅이지 않게 자동 감속.
        한가하면 sleep 없이 최고속으로 달린다.
    ``psutil``이 없으면 양보만 수행(여전히 사실상 최고속).

    환경변수 ``BACKTEST_AUTO_SPEED=0``이면 완전 비활성(순수 최고속).
    """

    def __init__(
        self,
        *,
        threshold: float = 88.0,
        max_sleep: float = 0.02,
        pause_path: Path | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> None:
        self._threshold = float(os.getenv("BACKTEST_CPU_THRESHOLD") or threshold)
        self._max_sleep = max_sleep
        self._pause_path = pause_path
        self._stop_check = stop_check
        # 분 단위 최소 재생 간격(ms) — HTS에서 캔들이 하나씩 순서대로 생성되는 것을
        # 사람이 볼 수 있게 하루를 적당한 시간에 걸쳐 재생(요구 2/5). 0이면 전속력.
        self._min_step_s = max(0.0, float(os.getenv("BACKTEST_STEP_MS") or 0) / 1000.0)
        self._enabled = (os.getenv("BACKTEST_AUTO_SPEED") or "1") != "0"
        self._psutil = None
        if self._enabled:
            try:
                import psutil  # type: ignore

                self._psutil = psutil
                psutil.cpu_percent(interval=None)   # 첫 표본 초기화(비차단)
            except Exception:  # noqa: BLE001
                self._psutil = None
        self._cpu = 0.0
        self._n = 0

    async def __call__(self, _minute: int) -> None:
        # 일시정지 게이트(요구 1) — 센티넬이 있는 동안 시각 전진을 막고 대기.
        if self._pause_path is not None:
            while self._pause_path.exists():
                if self._stop_check is not None and self._stop_check():
                    return
                await asyncio.sleep(0.15)
        # 항상 이벤트 루프에 양보 → 대시보드/IO 태스크가 굶지 않음.
        await asyncio.sleep(0)
        # 사람이 볼 수 있는 재생 속도(분당 최소 간격). 대시보드가 그 사이 상태를 기록한다.
        if self._min_step_s > 0:
            await asyncio.sleep(self._min_step_s)
        if not self._enabled or self._psutil is None:
            return
        # 매 5스텝마다 CPU 표본(표본 자체가 비용이라 과하지 않게).
        self._n += 1
        if self._n % 5 == 0:
            try:
                self._cpu = float(self._psutil.cpu_percent(interval=None))
            except Exception:  # noqa: BLE001
                self._cpu = 0.0
        if self._cpu >= self._threshold:
            # 초과분에 비례해 짧게 감속(최대 max_sleep).
            over = min(1.0, (self._cpu - self._threshold) / max(1.0, 100 - self._threshold))
            await asyncio.sleep(self._max_sleep * over)


# 자동 재시작 한도. **기본 0 = 무제한**(요구: 어떤 예외도 프로그램 전체 종료 금지 —
# 종료는 Ctrl+C 또는 kill_switch.py 로만). 실행 중 에러는 러너가 그 부분만 스킵하고
# 계속 진행하므로 여기 도달하는 건 setup/teardown 같은 드문 단계의 예외다. 영구적
# 오류(설정/임포트 등)라도 멈추지 않고 **정확한 원인**을 매 회차 출력하며 재시작한다.
# (디버깅 시 BACKTEST_MAX_RESTARTS 를 양수로 명시하면 그 횟수 후 멈춘다.)
MAX_RESTARTS = int(os.getenv("BACKTEST_MAX_RESTARTS") or 0)


def _kill_switch_path() -> Path:
    return Path(__file__).parents[1] / "state" / "KILL_SWITCH"


def _state_file_path() -> Path:
    return Path(__file__).parents[1] / "state" / "backtest_live.json"


def _foreign_engine_active() -> bool:
    """다른 백테스트 엔진이 **지금 살아서 돌고 있는지** 판정(단일 실행 보장).

    대시보드가 250ms마다 ``state/backtest_live.json``을 갱신하므로, 그 파일이
    2초 이내로 신선하고 그 안의 ``pid``가 우리 자신이 아니면 **다른 엔진이 가동
    중**이라는 뜻이다(예: traidair가 띄운 엔진이 도는 중에 터미널에서 또 실행).

    이 경우 두 엔진이 같은 ``state/`` 제어판(상태파일·STOP/PAUSE 센티넬)을 두고
    싸워 서로를 종료시키는 충돌이 난다 → 새 엔진은 시작을 양보한다.

    traidair의 정상 경로는 엔진을 띄우기 직전 상태파일을 지우므로(server.js
    ``startBacktest``) 이 함수는 False를 반환해 정상 시작된다. ``os.kill``로 pid
    생존을 확인하면 Windows에서 프로세스를 죽일 위험이 있어, 신선도(하트비트)와
    pid 비교만으로 안전하게 판정한다.
    """
    sp = _state_file_path()
    try:
        age = _walltime.time() - sp.stat().st_mtime
    except OSError:
        return False          # 파일 없음 = 가동 중인 엔진 없음
    if age >= 2.0:
        return False          # 2초 이상 정체 = 죽었거나 정지된 잔재
    try:
        pid = json.loads(sp.read_text(encoding="utf-8")).get("pid")
    except Exception:  # noqa: BLE001
        pid = None
    return bool(pid) and pid != os.getpid()


_LOGGING_READY = False


def _setup_logging() -> None:
    """로그 최적화(요구 3d): 콘솔은 INFO만(DEBUG 비활성), 파일 로그는 버퍼링.

    - 콘솔 핸들러: INFO (핫 루프 DEBUG 로그는 출력 안 함 → I/O 절감).
    - 파일 핸들러: ``MemoryHandler`` 로 감싸 capacity(기본 500건)마다, 그리고 ERROR
      발생 시 flush → 매 INFO 마다 디스크 쓰기를 하지 않는다. (재시작 시 중복 핸들러가
      붙지 않도록 idempotent.)
    """
    global _LOGGING_READY
    if _LOGGING_READY:
        return
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    root_logger.addHandler(console)

    try:
        log_dir = Path(__file__).parents[1] / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_h = logging.FileHandler(log_dir / "backtest.log", encoding="utf-8")
        file_h.setLevel(logging.INFO)
        file_h.setFormatter(fmt)
        buffered = logging.handlers.MemoryHandler(
            capacity=int(os.getenv("BACKTEST_LOG_BUFFER") or 500),
            flushLevel=logging.ERROR,
            target=file_h,
        )
        root_logger.addHandler(buffered)
    except Exception:  # noqa: BLE001 — 파일 로깅 실패는 비치명(콘솔은 유지)
        pass

    _LOGGING_READY = True


def main() -> int:
    """백테스트 진입점 — 어떤 예외도 프로그램을 종료시키지 않는다(요구 1·2·4).

    - 실행 중(분 루프/거래일 루프) 발생하는 에러는 러너가 **그 부분만 스킵하고 계속 진행**한다
      (``BacktestRunner._safe`` / ``_safe_run_day``, 요구 2).
    - 데이터 없는 날짜는 자동으로 다음 랜덤 날짜로 이동한다(러너 풀 기반 추첨).
    - 여기까지 올라온 예기치 못한 최상위 예외는 **전체 스택 트레이스 + 정확한 원인**을
      출력하고(요구 1·4), 짧은 대기 후 **무한 자동 재시작**한다(전체 종료 금지).
    - **유일한 종료 경로는 사용자 Ctrl+C 또는 kill_switch.py**(state/KILL_SWITCH 센티넬)다.
    """
    _setup_logging()

    # ── 단일 실행 보장(요구 3): 다른 백테스트 엔진이 가동 중이면 시작을 양보한다. ──
    # 한 번에 하나의 엔진만 돈다. 두 엔진이 같은 state/ 제어판을 두고 싸우면 서로를
    # 종료시키는 충돌(요구 1·2의 "start.bat 쪽 종료"·"즉시 종료")이 난다. 개발용으로
    # 의도적으로 병렬 실행하려면 BACKTEST_ALLOW_PARALLEL=1 로 우회한다.
    if (os.getenv("BACKTEST_ALLOW_PARALLEL") or "0") != "1" and _foreign_engine_active():
        log.warning("=" * 60)
        log.warning("⚠️ 이미 다른 백테스트 엔진이 실행 중입니다 — 중복 실행을 막기 위해 종료합니다.")
        log.warning("   (브라우저 HTS의 '▶ 백테스트' 버튼이 유일한 실행 경로입니다.")
        log.warning("    개발용 병렬 실행이 꼭 필요하면 BACKTEST_ALLOW_PARALLEL=1 로 우회하세요.)")
        log.warning("=" * 60)
        return 0

    kill_path = _kill_switch_path()
    # 시작 시 이전 실행의 kill 센티넬을 제거(과거 잔재로 즉시 종료되는 것 방지).
    try:
        kill_path.unlink()
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass

    attempts = 0
    while True:
        if kill_path.exists():
            log.info("⏹ kill_switch 감지 — 백테스트를 정상 종료합니다.")
            return 0
        try:
            return asyncio.run(_run_backtest())
        except KeyboardInterrupt:
            log.info("⏹ 사용자 중단(Ctrl+C) — 백테스트를 정상 종료합니다.")
            return 0
        except BaseException as exc:  # noqa: BLE001  (SystemExit 제외 위해 아래서 재판정)
            if isinstance(exc, SystemExit):
                # 명시적 종료 코드는 그대로 존중(테스트/스크립트 연동).
                raise
            attempts += 1
            tb = traceback.format_exc()
            log.critical("=" * 60)
            log.critical("❌ 백테스트 최상위 예외 발생 [%d회차] — 종료하지 않고 계속합니다", attempts)
            log.critical("   정확한 원인: %s: %s", type(exc).__name__, exc)
            log.critical("   스택 트레이스 전체:\n%s", tb)
            log.critical("=" * 60)
            # 콘솔이 닫혀도 원인이 남도록 영구 기록(요구 1).
            _write_crash_file(f"main-restart#{attempts}", tb)
            if kill_path.exists():
                log.info("⏹ kill_switch 감지 — 재시작하지 않고 종료합니다.")
                return 0
            if MAX_RESTARTS > 0 and attempts > MAX_RESTARTS:
                log.critical(
                    "연속 %d회 실패 — 자동 재시작 한도(BACKTEST_MAX_RESTARTS=%d) 소진하여 멈춤. "
                    "(기본값 0=무제한; 위 '정확한 원인'을 해결하세요)", attempts, MAX_RESTARTS,
                )
                # 종료코드 1 의 **유일한 의도적 지점** — 원인을 크래시 파일에도 남긴다(요구 2).
                _crash_dump("max-restarts-exhausted", exc)
                return 1
            delay = min(30, 2 ** min(attempts, 5))
            log.warning("⏳ %d초 후 자동 재시작합니다 (종료: Ctrl+C 또는 kill_switch.py).", delay)
            # 백오프 동안에도 kill_switch/Ctrl+C 를 짧게 폴링해 즉시 멈출 수 있게 한다.
            slept = 0.0
            try:
                while slept < delay:
                    if kill_path.exists():
                        log.info("⏹ kill_switch 감지 — 종료합니다.")
                        return 0
                    _walltime.sleep(0.25)
                    slept += 0.25
            except KeyboardInterrupt:
                log.info("⏹ 사용자 중단(Ctrl+C) — 백테스트를 정상 종료합니다.")
                return 0


async def _run_backtest() -> int:
    _setup_logging()

    # asyncio 태스크 예외가 **조용히 묻히지 않도록**(요구 3) 루프 예외 핸들러 설치.
    # 백그라운드 태스크(dashboard/stop_watcher)나 never-awaited 코루틴에서 난 예외도
    # "Task exception was never retrieved" 로 사라지는 대신 전체 스택으로 보고된다.
    def _aio_exc_handler(_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        msg = context.get("message") or "(no message)"
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
            return
        if exc is not None:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            log.error("⚠️ asyncio 태스크에서 삼켜질 뻔한 예외 포착: %s\n%s", msg, tb)
            _write_crash_file("asyncio-task", tb)
        else:
            log.error("⚠️ asyncio 경고: %s", msg)

    try:
        asyncio.get_running_loop().set_exception_handler(_aio_exc_handler)
    except Exception:  # noqa: BLE001 — 핸들러 설치 실패는 비치명
        pass

    root = Path(__file__).parents[1]
    cfg = KisClientConfig.from_files(project_root=root, mode_override=Mode.PAPER)

    # 백테스트 기간(요구): 기본 2023-01-01 ~ 오늘 전체. 2022 이전은 사용하지 않는다.
    start = _env_date("BACKTEST_START", date(2023, 1, 1))
    end = _env_date("BACKTEST_END", date.today())
    _floor = date(2023, 1, 1)
    if start < _floor:
        log.info("BACKTEST_START %s < 2023-01-01 — 2023-01-01 로 고정(2022 이전 미사용)", start)
        start = _floor
    # 실제 추첨은 아래에서 '로컬에 분봉이 있는 날짜'로만 좁혀진다(키움 수집 데이터 우선).

    # 로컬 분봉 저장소(§18) — 백테스트는 로컬에 있는 날짜만 사용.
    store = CandleStore(root)
    avail = store.available_dates()
    if avail:
        lo, hi = from_ymd(avail[0]), from_ymd(avail[-1])
        start, end = max(start, lo), min(end, hi)
        log.info("로컬 분봉 %d일 보유 (%s~%s) — 이 범위에서 추첨", len(avail), avail[0], avail[-1])
    else:
        log.warning(
            "로컬 분봉 없음 — 먼저 `python scripts/collect_candles.py`로 수집하세요. "
            "(traidair 분봉 폴백 시도하나 과거 일자는 비어 있을 수 있음)"
        )
    upath = root / "config" / "universe.json"
    names = name_map(load_universe(upath)) if upath.exists() else {}

    days_env = os.getenv("BACKTEST_DAYS")
    max_days = int(days_env) if days_env else None
    seed_env = os.getenv("BACKTEST_SEED")
    rng = random.Random(int(seed_env)) if seed_env else random.Random()

    # 가상 시작자금: **정확히 100만원으로 고정**(요구 3 — 절대 다른 값으로 바뀌지 않음).
    # 환경변수 오버라이드를 의도적으로 받지 않는다(1억 누수 버그/임의 잔고 차단).
    start_cash = 1_000_000

    bus = Bus()
    journal_dir = root / "data" / "journal"
    clock = SimClock(at_kst(start, time(9, 0)))
    JournalAgent(bus, journal_dir, clock=clock.now)

    # §19 장기 메모리 — 저널 집계(종목/패턴/시장등급 승률). 메타가 매일 마감에 rebuild.
    memory = MemoryStore(root)
    mview = memory.view  # 호출 시점의 최신 통계를 반영하는 팩토리

    # 학습부 노션 지식 — 세션 시작 시 각 에이전트가 참조(data/memory/notion_knowledge.json).
    # 백테스트는 완전 로컬(§21)이므로 네트워크 동기화 없이 기존 파일만 로드한다(없으면 빈 뷰).
    notion = NotionKnowledgeView.load(root)

    # 신용 적극 활용(§1.1·요구 2): 매수여력 = 가용현금 × 2 (절반은 신용 충당).
    # 실주문은 없고 PaperBroker가 신용/현금 분할을 가상 처리한다.
    from core.kis_client import PaperBroker
    credit_mult = float(os.getenv("BACKTEST_CREDIT_MULT") or 2.0)
    broker = PaperBroker(
        persist_path=None, start_cash=start_cash, credit_multiplier=credit_mult,
    )

    data_client = KisClient(cfg)
    replay = ReplayKisClient(
        data_client, clock, broker, account=cfg.account,
        candle_store=store, names=names,
    )

    # 실시간 현황 퍼블리셔 (traidair HTS '🎬 백테스트' 탭에서 표시).
    dashboard = BacktestDashboard(
        clock, broker, replay, bus,
        state_path=root / "state" / "backtest_live.json",
        start_cash=start_cash,
        mode=cfg.mode.value,
        memory=memory,
        # 진행 표시(요구 3): 설정한 거래일 수 → HTS "N일차 / M일". 무제한이면 None.
        total_days=max_days,
    )

    # 메타부 — 매일 마감 시 성과·토큰 관찰(meta.observation) + (paper) 진화 제안(learning.proposal).
    optimizer = OptimizerAgent(
        cfg.mode, bus, journal_dir,
        config_path=root / "config" / "strategy_params.yaml", clock=clock.now,
    )

    # ── 에이전트 (모두 가상 시계 주입) ──
    hl_cfg = HardLimitsConfig.from_file(root / "config" / "hard_limits.yaml")
    # 동시 보유 최대 종목 수 — HL-01을 상한으로 한다(요구 2: 3종목 보유 중 신규 매수 금지).
    max_positions = min(
        int(os.getenv("BACKTEST_MAX_POSITIONS") or hl_cfg.max_concurrent_positions),
        hl_cfg.max_concurrent_positions,
    )
    sig_params = SignalParams.from_file(root / "config" / "strategy_params.yaml")
    scr_params = ScreeningParams.from_file(root / "config" / "strategy_params.yaml")
    # 최적화(§21): 백테스트는 과거 DART 재현이 불가(traidair '최근 N일' 라우트)라 페널티가
    # 항상 0이다 → DART 조회(종목당 corpcode 네트워크 호출, 30회/스크리닝)를 끈다.
    scr_params = replace(scr_params, enable_dart=False)
    exit_params = ExitParams.from_file(root / "config" / "strategy_params.yaml")

    analyzer = SignalAnalyzer(sig_params)
    tracker = StopLossTracker()
    market_watch = MarketWatchAgent(replay, bus, clock=clock.now, notion_knowledge=notion)
    # §19 메모리 훅 주입 — 세션마다 최신 통계를 판단에 반영.
    screening = ScreeningAgent(
        replay, bus, scr_params, clock=clock.now,
        score_adjust=lambda c: mview().symbol_score_adjust(c),
        notion_knowledge=notion,
    )
    signal_agent = SignalAgent(
        replay, analyzer, bus, clock=clock.now,
        pattern_memory=lambda sig, passed: mview().pattern_confidence(sig, passed),
        notion_knowledge=notion,
    )
    selector = EntrySelector()

    current_grade = {"v": MarketGrade.GREEN}

    async def on_state(state: MarketState) -> None:
        current_grade["v"] = state.grade

    bus.subscribe(TOPIC_STATE, on_state)

    risk = RiskAgent(
        replay, HardLimitGate(hl_cfg, stoploss_tracker=tracker), bus,
        clock=clock.now, market_state_provider=lambda: current_grade["v"],
        grade_memory=lambda g: mview().grade_winrate(g),
        notion_knowledge=notion,
    )
    order = OrderAgent(replay, bus, clock=clock.now)
    pos_mgr = PositionManagerAgent(
        replay, bus, order, analyzer, exit_params, tracker, clock=clock.now,
    )

    async def on_entry(entry) -> None:
        await risk.review(entry)

    async def on_approved(approved) -> None:
        await order.execute(approved)

    bus.subscribe(TOPIC_ENTRY, on_entry)
    bus.subscribe(TOPIC_APPROVED, on_approved)

    # ── 러너 콜백 ──
    async def screen():
        return await screening.screen_once()

    # 진입 시점마다 스캔할 최대 후보 수 — 최강 종목이 신호 미발생/가용현금 부족으로
    # 진입 불가일 때 다음 강한 종목을 시도(§5.7 "최강 1종목"의 진입 가능 해석).
    max_entry_scan = int(os.getenv("BACKTEST_ENTRY_SCAN") or 8)

    async def try_enter(candidates) -> None:
        # 동시 보유가 한도(HL-01)에 도달하면 신규 매수 금지(요구 2).
        if pos_mgr.held_count() >= max_positions:
            return
        balance = await replay.get_balance()
        held_codes = {p.code for p in balance.positions if p.qty > 0}
        if len(held_codes) >= max_positions:
            return
        # 매수여력 = 가용현금 × 신용배수(§1.1). 1주도 못 사는 종목·이미 보유 중인
        # 종목은 후보에서 제외(가격 미상 0은 통과시켜 하류 게이트가 최종 판정).
        buy_power = int(balance.cash * credit_mult)
        affordable = [
            c for c in candidates
            if getattr(c, "code", "") not in held_codes
            and (getattr(c, "price", 0) <= 0 or 0 < c.price <= buy_power)
        ]
        ranked = selector.rank(affordable or candidates)
        if not ranked:
            return
        before = pos_mgr.held_count()
        for cand in ranked[:max_entry_scan]:
            if cand.code in held_codes:
                continue
            await signal_agent.analyze_symbol(cand.code)
            # Bus가 동기(gather)라 analyze_symbol 반환 시점에 진입 체결까지 끝나 있다.
            # 한 회차에 한 종목만 신규 진입하고 중단(다음 분에 다음 종목 시도).
            if pos_mgr.held_count() > before:
                break

    async def monitor() -> None:
        await pos_mgr.monitor_once()

    async def market_poll() -> None:
        await market_watch.poll_once()

    async def on_session_start(date_str: str) -> None:
        # 연속손절 카운터(HL-02)는 거래일마다 리셋. 보유 종목은 전날 15:20 EOD 강제청산으로
        # 이미 전량 비워져 있어(이월 없음, §5.7) 매 거래일은 무보유 상태로 시작한다.
        tracker.reset()
        current_grade["v"] = MarketGrade.GREEN
        dashboard.start_day(date_str)

    async def on_day_complete(result) -> None:
        await dashboard.end_day(result)
        # 마감 관찰/제안 (학습부·메타부 카드 데이터). 실패는 비치명.
        try:
            report = await optimizer.observe(result.date)
            await optimizer.propose(report)
        except Exception:  # noqa: BLE001
            log.debug("optimizer observe/propose 실패", exc_info=True)
        # §19 메타가 메모리 총괄 — 저널 재집계로 종목/패턴/등급 승률 갱신.
        try:
            memory.rebuild(journal_dir)
        except Exception:  # noqa: BLE001
            log.debug("memory rebuild 실패", exc_info=True)

    async def verify_trading_day(date_str: str) -> bool:
        # 로컬 저장소에 그 날짜 분봉이 있으면 거래일로 간주(§18).
        if store.available_dates():
            return store.has_date(date_str)
        # 폴백: traidair 분봉에 당일 데이터가 있으면 거래일.
        try:
            chart = await data_client.get_chart(PROBE_CODE, date=date_str, tf="1")
        except Exception:  # noqa: BLE001
            return False
        return chart.todayCount > 0

    async def backfill() -> int:
        """데이터 부족 시 Yahoo Finance에서 최근 분봉을 자동 수집한다(요구 3).

        Yahoo 1분봉은 최근 ~30일만 제공하므로 깊은 과거는 보완 불가하나, 최근 거래일은
        채울 수 있다. 수집 전후 로컬 보유 날짜 수의 **증가분**을 반환한다. 호출측
        (``runner._ensure_pool``)이 실패를 비치명으로 처리하므로 예외는 자유롭게 전파해도 된다.
        """
        before = set(store.available_dates())
        sys.path.insert(0, str(Path(__file__).parent))   # scripts/ 디렉터리(collect_candles)
        from collect_candles import collect as _collect
        log.info("부족한 분봉 자동 수집 시작 (Yahoo, 최근 30일) — 잠시 걸릴 수 있어요")
        await _collect(days=30, interval="1m", throttle=0.1, root=root, incremental=False)
        added = sorted(set(store.available_dates()) - before)
        log.info("자동 수집 결과: 신규 거래일 %d개 %s", len(added), added or "(없음 — Yahoo 제공 구간 밖)")
        return len(added)

    stop = asyncio.Event()

    # ── 정지/일시정지 센티넬(요구 1) ──
    # state/BACKTEST_STOP  : 리셋 버튼이 기록 → 워처가 감지해 stop_event 세팅(프로세스 종료).
    # state/BACKTEST_PAUSE : 토글 버튼이 일시정지 시 기록 → 페이서가 시각 전진을 막고 대기
    #                        (프로세스는 살아 있어 '이어서 진행'이 가능). 시작 시 둘 다 제거.
    stop_sentinel = root / "state" / "BACKTEST_STOP"
    pause_sentinel = root / "state" / "BACKTEST_PAUSE"
    kill_sentinel = root / "state" / "KILL_SWITCH"   # kill_switch.py 가 기록(요구: 종료 경로)
    for _s in (stop_sentinel, pause_sentinel):
        try:
            _s.unlink()   # 시작 시 이전 센티넬 제거
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            pass

    runner = BacktestRunner(
        clock, replay, broker, bus,
        start_date=start, end_date=end,
        screen=screen, try_enter=try_enter, monitor=monitor,
        market_poll=market_poll, on_session_start=on_session_start,
        on_day_complete=on_day_complete,
        verify_trading_day=verify_trading_day,
        start_cash=start_cash, rng=rng,
        # 시스템 부하 기반 자동 배속 + 일시정지 게이트(PAUSE 센티넬).
        pacer=AutoSpeedGovernor(pause_path=pause_sentinel, stop_check=stop.is_set),
        # 요구 2: 동시 최대 3종목. carry_over=True는 현금·누적손익만 연속으로 이어가며,
        # 보유 종목은 매일 15:20 EOD 강제청산으로 전량 비워 다음 거래일로 이월되지 않는다(§5.7).
        max_positions=max_positions,
        carry_over=True,
        # 데이터 부족 시 Yahoo 자동 수집(요구 3) — 보유 거래일이 부족할 때만 1회 시도.
        backfill=backfill,
    )

    # 사용자가 의도적으로 멈췄는지(Ctrl+C/센티넬) vs 데이터 없음으로 끝났는지 구분.
    # 데이터 없음(무제한 모드)으로 결과가 비면 종료하지 않고 재시도하기 위함(요구).
    user_stop = {"v": False}

    def _on_sigint(*_a: object) -> None:
        log.warning("중단 신호 — 현재 날짜 종료 후 정지")
        user_stop["v"] = True
        stop.set()

    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except (ValueError, OSError):
        pass   # 비메인 스레드 등에서는 무시

    async def _stop_watcher() -> None:
        while not stop.is_set():
            if stop_sentinel.exists():
                log.warning("정지 센티넬 감지 — 백테스트 중단")
                user_stop["v"] = True
                stop.set()
                break
            if kill_sentinel.exists():
                log.warning("kill_switch 센티넬 감지 — 백테스트 중단")
                user_stop["v"] = True
                stop.set()
                break
            await asyncio.sleep(0.2)

    log.info(
        "BACKTEST 시작: 기간=%s~%s, 일수=%s (소스: 지수=Yahoo/traidair, 종목=KIS, 공시=DART)",
        start, end, max_days or "무제한",
    )
    results: list = []
    async with replay:
        # 현황 퍼블리셔를 백그라운드로 동시 구동 (핫 루프 무영향, 파일 I/O만).
        dash_task = asyncio.create_task(dashboard.run(stop), name="dashboard")
        watch_task = asyncio.create_task(_stop_watcher(), name="stop_watcher")
        try:
            results = await runner.run_forever(stop, max_days=max_days)
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Ctrl+C/취소는 정상 중단 — 지금까지의 부분 결과를 그대로 리포트(요구 2).
            log.info("⏹ 실행 중단 요청 — 지금까지의 결과로 마무리합니다.")
            user_stop["v"] = True
            stop.set()
        except Exception:  # noqa: BLE001
            # 러너 내부는 이미 그 부분만 스킵하지만(요구 2), 만일을 위한 최종 안전망:
            # 예기치 못한 예외도 프로세스를 죽이지 않고 부분 결과로 리포트한다(요구 1·4).
            log.error(
                "러너 실행 중 예기치 못한 예외 — 부분 결과로 마무리(전체 스택 아래)",
                exc_info=True,
            )
            stop.set()
        finally:
            stop.set()
            for _t in (dash_task, watch_task):
                try:
                    await asyncio.wait_for(_t, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    _t.cancel()
            for _s in (stop_sentinel, pause_sentinel):
                try:
                    _s.unlink()
                except Exception:  # noqa: BLE001
                    pass

    try:
        _report(results)
    except Exception:  # noqa: BLE001
        log.warning("결과 리포트 생성 실패(비치명) — 백테스트 자체는 정상 종료", exc_info=True)

    # ── 정상 종료 vs 비정상 종료 구분(요구 2) ──
    # 데이터가 없어서/다 써서 결과가 비는 것은 **에러가 아니라 정상 종료**다(종료코드 0).
    # 예전에는 여기서 RuntimeError 를 던져 상위 재시작 루프가 무한 반복(또는
    # BACKTEST_MAX_RESTARTS>0 일 때 종료코드 1)했는데, 이것이 "원인 없이 죽는" 혼란의
    # 한 축이었다. 이제는 **왜 끝났는지** 분명히 알리고 깔끔히 0 으로 종료한다.
    if max_days is None and not results and not user_stop["v"]:
        log.warning("=" * 60)
        log.warning("✅ 재생할 데이터(로컬 분봉)가 없어 정상 종료합니다 (에러 아님, 종료코드 0).")
        log.warning(
            "   분봉을 수집하면 다음 실행부터 재생됩니다: "
            "`python scripts/collect_candles_kiwoom.py` (또는 collect_candles.py)"
        )
        log.warning("=" * 60)
        return 0
    return 0


def _report(results: list) -> None:
    if not results:
        log.warning("백테스트 결과 없음 (거래일 선택 실패 또는 데이터 없음)")
        return
    total = sum(r.pnl for r in results)
    wins = sum(1 for r in results if r.pnl > 0)
    entries = sum(r.n_entries for r in results)
    exits = sum(r.n_exits for r in results)
    win_rate = wins / len(results) * 100
    log.info("=" * 60)
    log.info("백테스트 요약: %d거래일", len(results))
    log.info("  총 손익      : %s원", f"{total:+,}")
    log.info("  승일/거래일  : %d/%d (%.1f%%)", wins, len(results), win_rate)
    log.info("  진입/청산 수 : %d / %d", entries, exits)
    log.info("=" * 60)
    for r in results:
        log.info(
            "  %s: %s원 (%+.2f%%) entries=%d exits=%d",
            r.date, f"{r.pnl:+,}", r.pnl_pct, r.n_entries, r.n_exits,
        )


if __name__ == "__main__":
    try:
        _code = main()
    except KeyboardInterrupt:
        # main() 의 재시작 루프가 이미 Ctrl+C 를 0 으로 처리하지만, 최후 안전망.
        _code = 0
    except BaseException as _e:  # noqa: BLE001 — 절대 조용히 죽지 않는다(요구 1)
        _crash_dump("toplevel", _e)
        _code = 1
    sys.exit(_code)
