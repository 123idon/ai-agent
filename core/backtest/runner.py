"""BacktestRunner — 랜덤 과거 거래일 리플레이 구동기 (CLAUDE.md §17).

랜덤 영업일을 골라 09:00→15:30을 분 단위로 전진시키며, 그 시점의 가상 시각으로
에이전트 콜백(스크리닝/진입/모니터/시장상황)을 호출한다. 에이전트 자체를 import하지
않고 **콜백 주입**으로 구동하므로 core 레이어가 agents에 의존하지 않는다(테스트 용이).

하루가 끝나면 ``DayResult``(가상자금 손익)를 산출하고, 다음 랜덤 영업일로 자동 진행한다.
거래일 여부(휴장/데이터 유무)는 ``verify_trading_day`` 콜백(실데이터 확인)이 권위.
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, time

from core.kis_client.paper_broker import PaperBroker
from core.messaging import Bus
from core.time_utils import (
    SimClock,
    at_kst,
    business_days,
    from_ymd,
    random_business_day,
    session_minutes,
    ymd,
)

from .replay_client import ReplayKisClient

log = logging.getLogger(__name__)

AsyncNoArg = Callable[[], Awaitable[None]]
AsyncScreen = Callable[[], Awaitable[Sequence[object]]]
AsyncEnter = Callable[[Sequence[object]], Awaitable[None]]
AsyncVerify = Callable[[str], Awaitable[bool]]


@dataclass
class DayResult:
    date: str
    start_cash: int
    end_value: int
    pnl: int
    pnl_pct: float
    n_entries: int = 0
    n_exits: int = 0
    note: str = ""


class BacktestRunner:
    def __init__(
        self,
        clock: SimClock,
        replay: ReplayKisClient,
        broker: PaperBroker,
        bus: Bus,
        *,
        start_date: date,
        end_date: date,
        screen: AsyncScreen,
        try_enter: AsyncEnter,
        monitor: AsyncNoArg,
        market_poll: AsyncNoArg | None = None,
        on_session_start: Callable[[str], Awaitable[None]] | None = None,
        on_day_complete: Callable[["DayResult"], Awaitable[None]] | None = None,
        verify_trading_day: AsyncVerify | None = None,
        start_cash: int = 100_000_000,
        step_minutes: int = 1,
        screen_every_minutes: int = 10,
        market_poll_every_minutes: int = 5,
        rng: random.Random | None = None,
        max_pick_attempts: int = 20,
        pacer: Callable[[int], Awaitable[None]] | None = None,
        max_positions: int = 1,
        carry_over: bool = False,
        backfill: Callable[[], Awaitable[int]] | None = None,
        min_pool_days: int = 5,
    ) -> None:
        self.clock = clock
        self.replay = replay
        self.broker = broker
        self.bus = bus
        self.start_date = start_date
        self.end_date = end_date
        self._screen = screen
        self._try_enter = try_enter
        self._monitor = monitor
        self._market_poll = market_poll
        self._on_session_start = on_session_start
        self._on_day_complete = on_day_complete
        self._verify = verify_trading_day
        self._start_cash = start_cash
        self._step = step_minutes
        self._screen_every = max(1, screen_every_minutes)
        self._market_every = max(1, market_poll_every_minutes)
        self._rng = rng or random.Random()
        self._max_attempts = max_pick_attempts
        # 자동 배속 페이서(§ traidair 연동): 분 단위 스텝마다 호출되어 시스템 부하에
        # 맞춰 페이싱한다. None이면 전속력(기존 동작 그대로).
        self._pacer = pacer
        # 동시 보유 최대 종목 수(HL-01, 요구 2). 1이면 단일 집중(기존 §5.7).
        self._max_positions = max(1, max_positions)
        # 잔고 이월(요구 3): True면 거래일이 바뀌어도 브로커를 리셋하지 않고 현금·보유·
        # 누적손익을 그대로 이어간다. False면 매일 start_cash로 리셋(기존 동작).
        self._carry_over = carry_over
        # 이월 시 당일 손익 기준선(전날 종료 순자산). None이면 첫날(=실측 순자산).
        # 이 기준선을 쓰면 일별 손익이 정확히 누적(오버나잇 갭 포함)되도록 telescoping된다.
        # NOTE: carry_over는 현금·누적손익 연속성만 이어받는다. **보유 종목은 절대 이월되지
        # 않는다** — 매 거래일 마감(15:20)에 EOD 강제청산으로 전량 비우기 때문이다(§5.7).
        self._carry_baseline: int | None = None
        # 데이터 부족 자동 보완 훅(요구 3): Yahoo 분봉을 1회 수집하고 **추가된 거래일 수**를
        # 반환한다. None이면 보완 없이 보유 데이터만 사용한다. 실패해도 비치명(계속 진행).
        self._backfill = backfill
        # 무제한(정지 시까지) 모드에서 데이터 보유 거래일이 이 값 미만이면 backfill을 시도한다.
        self._min_pool = max(1, min_pool_days)

        # 일 단위 카운터 (bus 구독으로 집계).
        self._n_entries = 0
        self._n_exits = 0
        self.bus.subscribe("order.event", self._count_order)
        self.bus.subscribe("signal.exit", self._count_exit)

    async def _count_order(self, ev: object) -> None:
        side = getattr(ev, "side", None)
        if getattr(side, "value", side) == "buy":
            self._n_entries += 1

    async def _count_exit(self, _ev: object) -> None:
        self._n_exits += 1

    # ─────────────────────────── 날짜 선택 ───────────────────────────

    async def pick_trading_day(self) -> str | None:
        """[start, end]에서 랜덤 영업일을 고르되, 실데이터로 거래일을 재확인.

        ``verify_trading_day`` 콜백이 False(휴장/데이터 없음)이면 다른 날짜를 재추첨.
        """
        for _ in range(self._max_attempts):
            d = random_business_day(self.start_date, self.end_date, rng=self._rng)
            ds = ymd(d)
            if self._verify is None:
                return ds
            try:
                if await self._verify(ds):
                    return ds
                log.info("skip non-trading/empty day %s — 재추첨", ds)
            except Exception:  # noqa: BLE001
                log.warning("verify_trading_day(%s) 실패 — 재추첨", ds, exc_info=True)
        log.error("거래일 선택 실패 (%d회 시도)", self._max_attempts)
        return None

    # ─────────────────────────── 1일 실행 ───────────────────────────

    async def run_one_day(
        self, date_str: str, stop_event: asyncio.Event | None = None,
    ) -> DayResult:
        d = from_ymd(date_str)
        # 잔고 이월(요구 3): carry_over면 리셋하지 않고 전날 현금·보유·누적손익을 이어간다.
        if not self._carry_over:
            self.broker.reset(start_cash=self._start_cash)
        self.replay.set_session(date_str)
        self.clock.set(at_kst(d, time(9, 0)))
        self._n_entries = 0
        self._n_exits = 0
        if self._on_session_start is not None:
            await self._on_session_start(date_str)

        # 당일 손익 기준선. 이월 시 전날 종료 순자산을 그대로 써서 오버나잇 갭까지
        # 당일 손익에 포함시킨다(누적손익이 정확히 telescoping). 첫날/비이월은 실측 순자산.
        if self._carry_over and self._carry_baseline is not None:
            day_start_value = self._carry_baseline
        else:
            day_start_value = (await self.replay.get_balance()).totalEval
        log.info("BACKTEST DAY %s 시작 (start_value=%s)", date_str, f"{day_start_value:,}")
        screen_cache: Sequence[object] = []
        last_screen_min = -10**9
        last_market_min = -10**9

        for i, t in enumerate(session_minutes(d, step_minutes=self._step)):
            # 정지 요청 즉시 반영 — 분 루프 한가운데서도 멈춘다(요구 1).
            if stop_event is not None and stop_event.is_set():
                log.info("정지 요청 감지 — %s 진행 중단", date_str)
                break
            self.clock.set(t)
            minute = i * self._step

            if self._pacer is not None:
                await self._safe("pacer", self._pacer(minute))

            if self._market_poll is not None and (
                minute - last_market_min >= self._market_every
            ):
                last_market_min = minute
                await self._safe("market_poll", self._market_poll())

            held = await self._held_count()
            # 보유 종목이 있으면 항상 청산 점검(다종목 동시 보유 지원, 요구 2).
            if held > 0:
                await self._safe("monitor", self._monitor())
            # 보유 종목 수가 한도 미만이면 신규 진입 시도(요구 2: 3종목 미만일 때만).
            if held < self._max_positions:
                if not screen_cache or (minute - last_screen_min >= self._screen_every):
                    last_screen_min = minute
                    screen_cache = await self._safe_screen()
                await self._safe("try_enter", self._try_enter(screen_cache))

        # 장 종료 정리 — 보유분은 **어떤 조건에서도 예외 없이** 전량 강제청산한다(§5.7).
        # 세션 분 루프가 이미 15:20에 EOD 청산하지만, 잔여/입양 포지션까지 확실히 비워
        # 보유 종목이 다음 거래일로 이월되지 않도록 보장한다(무보유가 될 때까지 반복).
        self.clock.set(at_kst(d, time(15, 25)))
        for _ in range(5):
            if await self._is_flat():
                break
            await self._safe("monitor", self._monitor())
        if not await self._is_flat():
            log.error("EOD 강제청산 후에도 보유 잔존 — %s (이월 위험)", date_str)

        bal = await self.replay.get_balance()
        end_value = bal.totalEval
        # 다음 거래일의 손익 기준선으로 이월(전날 종료 순자산).
        self._carry_baseline = end_value
        pnl = end_value - day_start_value
        pct = (pnl / day_start_value * 100) if day_start_value else 0.0
        result = DayResult(
            date=date_str, start_cash=day_start_value, end_value=end_value,
            pnl=pnl, pnl_pct=pct, n_entries=self._n_entries, n_exits=self._n_exits,
        )
        log.info(
            "BACKTEST DAY %s 종료: pnl=%s (%+.2f%%) entries=%d exits=%d",
            date_str, f"{pnl:+,}", pct, self._n_entries, self._n_exits,
        )
        return result

    async def _is_flat(self) -> bool:
        _cash, positions = self.broker.snapshot()
        return not any(p.qty > 0 for p in positions.values())

    async def _held_count(self) -> int:
        _cash, positions = self.broker.snapshot()
        return sum(1 for p in positions.values() if p.qty > 0)

    async def _safe_screen(self) -> Sequence[object]:
        try:
            return await self._screen()
        except Exception:  # noqa: BLE001
            log.warning("screen 콜백 실패", exc_info=True)
            return []

    @staticmethod
    async def _safe(label: str, coro: Awaitable[None]) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001
            log.warning("%s 콜백 실패", label, exc_info=True)

    # ─────────────────────────── 다일 루프 ───────────────────────────

    async def run_days(self, n_days: int) -> list[DayResult]:
        results: list[DayResult] = []
        _nostop = asyncio.Event()
        for _ in range(n_days):
            ds = await self.pick_trading_day()
            if ds is None:
                break
            r = await self._safe_run_day(ds, _nostop)   # 에러 시 그 날짜만 스킵(요구 4)
            if r is None:
                continue
            results.append(r)
            if self._on_day_complete is not None:
                await self._safe("on_day_complete", self._on_day_complete(r))
        return results

    @staticmethod
    def _pool_brief(pool: Sequence[str]) -> str:
        """진단 로그용 — 날짜 풀을 너무 길지 않게 요약(앞 10개 + …범위)."""
        pool = list(pool)
        if len(pool) <= 12:
            return str(pool)
        return f"[{pool[0]} … {pool[-1]}] (앞 10개: {pool[:10]})"

    async def _verified_days(self, limit: int | None) -> list[str]:
        """[start, end] 영업일(주말 제외)을 랜덤 셔플 후 **데이터 보유 거래일**만
        distinct 수집한다. ``limit``개를 채우면 즉시 멈춰 불필요한 검증을 피한다.

        휴장/데이터 없음은 ``verify_trading_day``(실데이터)가 권위적으로 거른다 →
        공휴일/주말이 끼지 않은 정확한 거래일만 남는다(요구 1).
        """
        pool = business_days(self.start_date, self.end_date)
        self._rng.shuffle(pool)
        out: list[str] = []
        for d in pool:
            if limit is not None and len(out) >= limit:
                break
            ds = ymd(d)
            if self._verify is None:
                out.append(ds)
                continue
            try:
                if await self._verify(ds):
                    out.append(ds)
            except Exception:  # noqa: BLE001
                log.warning("verify_trading_day(%s) 실패 — 제외", ds, exc_info=True)
        return out

    async def _safe_run_day(
        self, date_str: str, stop_event: asyncio.Event,
    ) -> DayResult | None:
        """``run_one_day``를 감싸 **어떤 예외에도 백테스트 전체가 죽지 않게** 한다(요구 4).

        에러가 난 그 날짜만 스킵하고 ``None``을 반환한다. 에러 내용은 로그에 남기고
        다음 날짜로 이동한다(절대 비정상 종료 없음).
        """
        try:
            return await self.run_one_day(date_str, stop_event=stop_event)
        except Exception:  # noqa: BLE001
            log.error(
                "거래일 %s 실행 중 에러 — 이 날짜만 스킵하고 백테스트는 계속 진행(요구 4)",
                date_str, exc_info=True,
            )
            return None

    async def _ensure_pool(self, want: int | None) -> list[str]:
        """데이터 보유 거래일 풀을 만든다. 부족하면 backfill(Yahoo 자동 수집)을 1회 시도한다(요구 3).

        ``want``개(또는 무제한이면 ``min_pool``개)를 채우지 못하면 자동 수집을 시도한 뒤
        풀을 재구성한다. 수집 실패는 비치명 — 보유 데이터로 계속 진행한다.
        """
        pool = await self._verified_days(want)
        target = want if want is not None else self._min_pool
        if len(pool) < target and self._backfill is not None:
            log.warning(
                "데이터 보유 거래일 %d개 < 필요 %d개 — Yahoo 자동 수집 시도(요구 3)",
                len(pool), target,
            )
            try:
                added = await self._backfill()
                log.info("자동 수집 완료: 거래일 %d개 추가 — 풀 재구성", added)
            except Exception:  # noqa: BLE001
                log.warning("자동 수집 실패(비치명) — 보유 데이터로 계속 진행", exc_info=True)
            pool = await self._verified_days(want)
        return pool

    async def run_forever(
        self, stop_event: asyncio.Event, *, max_days: int | None = None,
    ) -> list[DayResult]:
        results: list[DayResult] = []

        # 무제한(max_days=None): 데이터 보유 거래일 풀에서 랜덤 재생을 **정지 시까지** 반복한다.
        # ★ 핵심 수정(요구 1·2): 매 회차 random_business_day+검증(20회 시도 후 None→종료) 대신,
        #   미리 확보한 '데이터 있는 날짜'만 랜덤 추첨한다 → 빈 날짜로 인한 조기 자동 종료가
        #   원천 차단된다(예: 4일 만에 혼자 멈추던 버그). 정지/Ctrl+C로만 끝난다.
        if max_days is None:
            pool = await self._ensure_pool(None)
            if not pool:
                log.error(
                    "데이터 보유 거래일이 없습니다 — 백테스트를 시작할 수 없음 "
                    "(`python scripts/collect_candles.py`로 분봉을 먼저 수집하세요)",
                )
                return results
            log.info("=" * 60)
            log.info(
                "📋 무제한 백테스트 시작 — 데이터 보유 거래일 %d개에서 랜덤 재생(정지 시까지 계속)",
                len(pool),
            )
            log.info("   가용 날짜 풀(%d일): %s", len(pool), self._pool_brief(pool))
            log.info("=" * 60)
            stop_reason = "정지 요청"
            while not stop_event.is_set():
                ds = self._rng.choice(pool)
                day_no = len(results) + 1
                log.info("▶ %d일차 시작 (날짜: %s) — 전체 진행: %d일 / 목표 무제한",
                         day_no, ds, len(results))
                r = await self._safe_run_day(ds, stop_event)
                if stop_event.is_set():
                    log.info("⏹ %d일차 중단 (사유: 정지 요청) — %s", day_no, ds)
                    break
                if r is None:
                    log.warning("✖ %d일차 종료 (사유: 데이터없음/에러) — %s 스킵, 다음 날짜로 이동",
                                day_no, ds)
                    continue
                results.append(r)
                log.info("✔ %d일차 종료 (사유: 정상마감, 날짜: %s, pnl=%+d) — 전체 진행: %d일 / 목표 무제한",
                         day_no, ds, r.pnl, len(results))
                if self._on_day_complete is not None:
                    await self._safe("on_day_complete", self._on_day_complete(r))
            log.info("🏁 백테스트 루프 종료 — 완주 %d일 (사유: %s)", len(results), stop_reason)
            return results

        # 거래일 수 지정(요구 1·2): **정확히 max_days 거래일**(데이터 있는 날짜만 카운트)을 진행.
        # 1) 데이터 보유 거래일을 distinct로 우선 확보(공휴일/주말 자동 제외, 부족 시 자동 수집).
        # 2) 보유 거래일이 max_days보다 적으면 부족분은 같은 거래일을 반복 재생해 채운다
        #    → '설정한 거래일 수 = 실제 진행된 날짜 수'를 항상 보장(조기 종료 버그 제거).
        verified = await self._ensure_pool(max_days)
        if not verified:
            log.error("거래일 선택 실패 — [%s~%s] 데이터 보유 거래일 없음",
                      self.start_date, self.end_date)
            return results
        log.info("=" * 60)
        log.info("📋 백테스트 시작 — 목표 %d거래일", max_days)
        log.info("   데이터 보유 거래일 풀: %d개 %s", len(verified), self._pool_brief(verified))
        if len(verified) < max_days:
            # 데이터가 목표보다 적으면 부족분은 같은 거래일을 반복 재생해 목표 일수를 채운다.
            log.warning(
                "⚠ 데이터 보유 거래일 %d개 < 목표 %d개 — 부족분 %d일은 보유 거래일 반복 재생으로 채웁니다",
                len(verified), max_days, max_days - len(verified),
            )
        log.info("   진행 예정: 목표 %d일 (고유 데이터일 %d개, 반복 재생 %d회)",
                 max_days, len(verified), max(0, max_days - len(verified)))
        log.info("=" * 60)

        i = 0
        # 연속 실패(데이터 없음/에러) 가드(요구 4): 모든 거래일이 연속 실패하면 무한 루프 대신
        # 종료한다. 정상 진행(데이터일)이 하나라도 나오면 카운터가 리셋된다.
        consecutive_failures = 0
        stop_reason = "목표 달성"
        while len(results) < max_days and not stop_event.is_set():
            ds = verified[i % len(verified)]
            i += 1
            day_no = len(results) + 1
            log.info("▶ %d일차 시작 (날짜: %s) — 전체 진행: %d일 / 목표 %d일",
                     day_no, ds, len(results), max_days)
            r = await self._safe_run_day(ds, stop_event)
            if stop_event.is_set():
                # 정지로 중단된 미완료 일자는 결과에 넣지 않는다(부분일 왜곡 방지).
                log.info("⏹ %d일차 중단 (사유: 정지 요청) — %s, 루프 종료", day_no, ds)
                stop_reason = "정지 요청(Ctrl+C/센티넬)"
                break
            if r is None:
                consecutive_failures += 1
                log.warning("✖ %d일차 종료 (사유: 데이터없음/에러) — %s 스킵 (연속실패 %d/%d)",
                            day_no, ds, consecutive_failures, len(verified))
                if consecutive_failures >= len(verified):
                    log.error(
                        "모든 거래일(%d개)이 연속 실패 — 더 진행 불가, 백테스트 종료",
                        len(verified),
                    )
                    stop_reason = "전 거래일 연속 실패(데이터/에러)"
                    break
                continue
            consecutive_failures = 0
            results.append(r)
            log.info("✔ %d일차 종료 (사유: 정상마감, 날짜: %s, pnl=%+d) — 전체 진행: %d일 / 목표 %d일",
                     day_no, ds, r.pnl, len(results), max_days)
            if self._on_day_complete is not None:
                await self._safe("on_day_complete", self._on_day_complete(r))
            if len(results) < max_days and not stop_event.is_set():
                nxt = verified[i % len(verified)]
                log.info("➡ 다음 날짜 선택: %s", nxt)
        log.info("=" * 60)
        log.info("🏁 백테스트 루프 종료 — 완주 %d일 / 목표 %d일 (사유: %s)",
                 len(results), max_days, stop_reason)
        log.info("=" * 60)
        return results
