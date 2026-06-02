"""백테스트 실시간 현황 퍼블리셔 (CLAUDE.md §17).

핫 리플레이 루프를 **전혀 건드리지 않고** 진행 상황을 traidair HTS에 노출한다:
- Bus 토픽 구독으로 스크리닝/신호/리스크/체결/청산/시장상태를 O(1)로 수집(거의 무비용).
- 별도 백그라운드 태스크가 일정 간격(기본 250ms 벽시계)으로 ``state/backtest_live.json``
  을 원자적으로 기록한다. traidair는 이 파일을 ``GET /api/backtest/state``로 서빙한다.

에이전트 타입을 import하지 않고 payload는 duck-typing(getattr)으로 다룬다 →
core 레이어가 agents에 의존하지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from core.time_utils import SimClock

log = logging.getLogger(__name__)


def _v(x: Any) -> Any:
    """enum이면 .value, 아니면 그대로."""
    return getattr(x, "value", x)


def _hhmmss(dt: Any) -> str:
    try:
        return dt.strftime("%H:%M:%S")
    except Exception:  # noqa: BLE001
        return ""


class BacktestDashboard:
    def __init__(
        self,
        clock: SimClock,
        broker: Any,
        replay: Any,
        bus: Any,
        *,
        state_path: Path,
        start_cash: int,
        mode: str = "paper",
        memory: Any = None,
        push_interval: float = 0.25,
        total_days: int | None = None,
    ) -> None:
        self._clock = clock
        self._broker = broker
        self._replay = replay
        self._state_path = Path(state_path)
        self._start_cash = start_cash
        self._mode = mode
        self._memory = memory   # §19 MemoryStore (best/worst 패턴, 등급 통계)
        self._interval = push_interval
        # 진행 표시(요구 3): 설정한 총 거래일 수(무제한이면 None)와 현재 진행 중인 일차(1-based).
        # HTS가 "3일차 / 10일" 처럼 보여줄 수 있게 스냅샷 cumulative에 노출한다.
        self._total_days = total_days
        self._day_no = 0

        # 당일 상태 (start_day에서 초기화)
        self._candidates: list[dict] = []
        self._trades: list[dict] = []
        # 누적 거래 내역 (런 전체, 날짜 바뀌어도 유지 — 거래 탭 누적 표시용).
        self._all_trades: list[dict] = []
        # 거래 기록 영구 보존(중간에 멈춰도) — data/journal/backtest_trades.jsonl append-only.
        try:
            _root = self._state_path.resolve().parent.parent
        except Exception:  # noqa: BLE001
            _root = Path(".")
        self._trades_journal = _root / "data" / "journal" / "backtest_trades.jsonl"
        self._last_signal: dict | None = None
        self._last_risk: dict | None = None
        self._last_order: dict | None = None
        self._market: dict | None = None
        self._day_date: str = clock.date_str
        self._name_map: dict[str, str] = {}
        # 당일 시작 자산(오늘손익 산출용) — 일 시작 시 플랫이므로 현금=자산.
        self._day_start_equity: float = float(start_cash)
        # 차트 포커스 종목(스티키) — 매일 리셋.
        self._focus_code: str | None = None

        # 누적 상태 (런 전체)
        self._day_results: list[dict] = []
        self._all_exit_pnls: list[float] = []
        self._proposals: list[dict] = []
        self._observation: dict | None = None
        self._running = True

        bus.subscribe("screening.candidates", self._on_candidate)
        bus.subscribe("signal.analysis", self._on_analysis)
        bus.subscribe("signal.exit", self._on_exit)
        bus.subscribe("risk.decision.approved", self._on_approved)
        bus.subscribe("risk.decision.rejected", self._on_rejected)
        bus.subscribe("order.event", self._on_order)
        bus.subscribe("order.failed", self._on_order_failed)
        bus.subscribe("market.state", self._on_market)
        bus.subscribe("learning.proposal", self._on_proposal)
        bus.subscribe("meta.observation", self._on_observation)

    # ─────────────────────────── 일 경계 ───────────────────────────

    def start_day(self, date_str: str) -> None:
        self._day_date = date_str
        # 현재 진행 일차(1-based) = 이미 완료한 거래일 수 + 1. 데이터 있는 날짜만 카운트되며
        # (run_one_day로 완료된 날만 _day_results에 적재), 무데이터 날짜는 러너가 스킵하므로
        # 여기 도달하지 않는다 → "N일차" 카운트가 데이터 거래일 기준으로 정확하다(요구 3).
        self._day_no = len(self._day_results) + 1
        self._candidates = []
        self._trades = []
        self._last_signal = None
        self._last_risk = None
        self._last_order = None
        self._market = None
        self._focus_code = None
        # 일 시작 시점 순자산 — 당일 손익 기준선. 잔고 이월(요구 3)에서는 전날 보유
        # 평가까지 포함한 순자산(현금+보유−신용)을 기준으로 당일 손익을 잰다.
        equity_fn = getattr(self._broker, "equity", None)
        if callable(equity_fn):
            self._day_start_equity = float(equity_fn())
        else:
            self._day_start_equity = float(getattr(self._broker, "cash", self._start_cash))

    async def end_day(self, result: Any) -> None:
        self._day_results.append({
            "date": getattr(result, "date", self._day_date),
            "pnl": int(getattr(result, "pnl", 0)),
            "pnl_pct": float(getattr(result, "pnl_pct", 0.0)),
            "n_entries": int(getattr(result, "n_entries", 0)),
            "n_exits": int(getattr(result, "n_exits", 0)),
        })

    def _record_trade(self, rec: dict) -> None:
        """거래 1건을 당일(`_trades`)·누적(`_all_trades`)에 적재하고 저널에 영구 기록.

        날짜가 바뀌어도 ``_all_trades`` 는 유지되어 거래 탭에 전체 기간이 누적 표시되고,
        ``data/journal/backtest_trades.jsonl`` 에 즉시 append 되어 중간에 멈춰도 보존된다.
        """
        rec.setdefault("date", self._day_date)
        self._trades.append(rec)
        self._all_trades.append(rec)
        # 메모리 폭주 방지(극단적 장기 런) — 최근 5000건만 메모리 보존(저널엔 전부 남음).
        if len(self._all_trades) > 5000:
            self._all_trades = self._all_trades[-5000:]
        try:
            self._trades_journal.parent.mkdir(parents=True, exist_ok=True)
            with self._trades_journal.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    # ─────────────────────────── Bus 핸들러 ───────────────────────────

    async def _on_candidate(self, p: Any) -> None:
        code = getattr(p, "code", "")
        name = getattr(p, "name", "")
        if name:
            self._name_map[code] = name
        bd = getattr(p, "breakdown", None)
        self._candidates.append({
            "code": code, "name": name,
            "score": round(float(getattr(p, "score", 0.0)), 1),
            "themes": list(getattr(p, "themes", ()) or ()),
            "breakdown": {k: round(float(v), 1) for k, v in (bd or {}).items()}
            if isinstance(bd, dict) else {},
        })
        # 점수순 상위만 유지 (중복 방지: code 기준 최신)
        seen: dict[str, dict] = {}
        for c in self._candidates:
            seen[c["code"]] = c
        self._candidates = sorted(
            seen.values(), key=lambda c: c["score"], reverse=True,
        )[:30]

    async def _on_analysis(self, p: Any) -> None:
        inds = []
        for iv in getattr(p, "indicators", ()) or ():
            inds.append({
                "name": getattr(iv, "name", ""),
                "passed": bool(getattr(iv, "passed", False)),
                "detail": getattr(iv, "detail", ""),
                "value": getattr(iv, "value", None),
            })
        self._last_signal = {
            "symbol": getattr(p, "symbol", ""),
            "signal": _v(getattr(p, "signal", "")),
            "scoreCount": int(getattr(p, "score_count", 0)),
            "indicators": inds,
            "reason": getattr(p, "reason", ""),
            "time": _hhmmss(getattr(p, "timestamp", None)),
        }

    async def _on_exit(self, p: Any) -> None:
        pnl_pct = float(getattr(p, "pnl_pct", 0.0))
        self._all_exit_pnls.append(pnl_pct)
        code = getattr(p, "symbol", "")
        self._record_trade({
            "kind": "exit",
            "code": code, "name": self._name_map.get(code, ""),
            "exitKind": _v(getattr(p, "kind", "")),
            "qty": int(getattr(p, "qty", 0)),
            "price": int(getattr(p, "price", 0)),
            "pnlPct": round(pnl_pct * 100, 2),
            "counter": _v(getattr(p, "counter", "")),
            "reason": getattr(p, "reason", ""),
            "time": _hhmmss(getattr(p, "timestamp", None)),
        })

    async def _on_approved(self, p: Any) -> None:
        self._last_risk = {
            "decision": "APPROVE",
            "symbol": getattr(p, "symbol", ""),
            "qty": int(getattr(p, "qty", 0)),
            "price": int(getattr(p, "price", 0)),
            "use_credit": bool(getattr(p, "use_credit", False)),
            "reason": getattr(p, "reason", ""),
            "time": _hhmmss(getattr(p, "timestamp", None)),
        }

    async def _on_rejected(self, p: Any) -> None:
        viols = getattr(p, "violations", ()) or ()
        first = viols[0] if viols else None
        self._last_risk = {
            "decision": "REJECT",
            "symbol": getattr(p, "symbol", ""),
            "rule": getattr(first, "rule_id", "") if first else "",
            "reason": getattr(first, "reason", "") if first else "",
            "time": _hhmmss(getattr(p, "timestamp", None)),
        }

    async def _on_order(self, p: Any) -> None:
        side = _v(getattr(p, "side", ""))
        code = getattr(p, "symbol", "")
        self._last_order = {
            "side": side, "code": code, "name": self._name_map.get(code, ""),
            "qty": int(getattr(p, "qty", 0)), "price": int(getattr(p, "price", 0)),
            "status": "filled", "msg": getattr(p, "msg", ""),
            "time": _hhmmss(getattr(p, "timestamp", None)),
        }
        if side == "buy":
            self._record_trade({
                "kind": "entry",
                "code": code, "name": self._name_map.get(code, ""),
                "qty": int(getattr(p, "qty", 0)),
                "price": int(getattr(p, "price", 0)),
                "use_credit": bool(getattr(p, "use_credit", False)),
                "time": _hhmmss(getattr(p, "timestamp", None)),
            })

    async def _on_order_failed(self, p: Any) -> None:
        code = getattr(p, "symbol", "")
        self._last_order = {
            "side": "", "code": code, "name": self._name_map.get(code, ""),
            "status": "failed", "msg": getattr(p, "error", ""),
            "time": _hhmmss(getattr(p, "timestamp", None)),
        }

    async def _on_market(self, p: Any) -> None:
        self._market = {
            "grade": _v(getattr(p, "grade", "")),
            "reason": getattr(p, "reason", ""),
            "kospi": getattr(p, "kospi_chg_pct", None),
            "kosdaq": getattr(p, "kosdaq_chg_pct", None),
            "vix": getattr(p, "vix", None),
            "usdkrw": getattr(p, "usdkrw_chg_pct", None),
        }

    async def _on_proposal(self, p: Any) -> None:
        pid = getattr(p, "proposal_id", None)
        if pid is None:
            return   # 비-제안 페이로드(DailySummary 등) 무시
        self._proposals.append({
            "id": pid,
            "kind": _v(getattr(p, "kind", "")),
            "rationale": getattr(p, "rationale", ""),
        })
        self._proposals = self._proposals[-10:]

    async def _on_observation(self, p: Any) -> None:
        perf = getattr(p, "performance", None)
        tokens = getattr(p, "tokens", None)
        def g(o: Any, k: str, d: Any = None) -> Any:
            return getattr(o, k, (o.get(k, d) if isinstance(o, dict) else d)) if o is not None else d
        self._observation = {
            "winRate": g(perf, "win_rate"),
            "profitFactor": g(perf, "profit_factor"),
            "payoff": g(perf, "payoff"),
            "trades": g(perf, "trades"),
            "tokenCalls": g(tokens, "total_calls", 0),
        }

    # ─────────────────────────── 스냅샷 ───────────────────────────

    async def _balance(self) -> dict:
        """가상잔고 요약 — HTS 보유 탭 '모의잔고/오늘손익/한도' 연동용 (요구 3)."""
        try:
            bal = await self._replay.get_balance()
            cash, total_eval, total_pnl = bal.cash, bal.totalEval, bal.totalPnl
        except Exception:  # noqa: BLE001
            cash = total_eval = self._start_cash
            total_pnl = 0
        today_pnl = int(total_eval - self._day_start_equity)
        credit_used = 0
        cu_fn = getattr(self._broker, "credit_used", None)
        if callable(cu_fn):
            credit_used = int(cu_fn())
        # 신용 포함 매수여력(가용현금 × 신용배수).
        mult = float(getattr(self._broker, "credit_multiplier", 1.0) or 1.0)
        buy_power = int(cash * mult)
        return {
            "cash": int(cash),               # 주문 가능 현금
            "totalEval": int(total_eval),    # 순자산(현금+보유평가−신용) = 가상잔고
            "totalPnl": int(total_pnl),      # 보유 평가손익(미실현)
            "startCash": int(self._start_cash),
            "todayPnl": today_pnl,           # 당일 손익(시작 순자산 대비)
            "creditUsed": credit_used,       # 사용 중인 신용(차입) 총액
            # 매수여력 한도(가용현금 × 신용배수). 신용 적극 활용(§1.1).
            "creditLimit": buy_power,
            "marginLimit": buy_power,
        }

    async def _focus_chart(self, positions: list[dict]) -> dict | None:
        """HTS 실시간 차트 재생용 — 에이전트가 '지금 보는' 종목의 분봉을 가상 시각까지.

        포커스 우선순위: 보유 종목 → 직전 분석 신호 종목 → 최고점 스크리닝 후보.
        ``get_chart``가 이미 현재 sim 시각 이전(<=) 분봉만 반환하므로, 매 스냅샷마다
        오늘 봉이 한 개씩 늘어난다 → HTS에서 캔들이 실시간 생성되는 것처럼 보인다.
        """
        code = None
        if positions:
            code = positions[0]["code"]
        elif self._last_signal and self._last_signal.get("symbol"):
            code = self._last_signal["symbol"]
        elif self._focus_code:
            code = self._focus_code            # 직전 포커스 유지(무보유·신규 신호 없을 때 깜빡임 방지)
        elif self._candidates:
            code = self._candidates[0]["code"]
        if not code:
            return None
        self._focus_code = code
        try:
            ch = await self._replay.get_chart(code, tf="1")
        except Exception:  # noqa: BLE001
            return None
        today = [c for c in ch.candles if not getattr(c, "isPrev", False)]
        prevs = [c for c in ch.candles if getattr(c, "isPrev", False)]
        if prevs:
            prev_close = int(prevs[-1].c)
        elif today:
            prev_close = int(today[0].o)
        else:
            prev_close = 0
        # 전일 봉(전체) + 당일 봉(가상 시각까지)을 연속으로 — HTS가 추세를 이어 그린다.
        # prevCount 로 경계를 알려 HTS가 09:00 구분선·전일 흐림 처리를 한다.
        candles = [
            {"t": c.t, "o": int(c.o), "h": int(c.h),
             "l": int(c.l), "c": int(c.c), "v": int(c.v)}
            for c in (prevs + today)
        ]
        return {
            "code": code,
            "name": self._name_map.get(code, ""),
            "date": self._day_date,
            "prevDate": getattr(ch, "prevDate", "") or "",
            "prevClose": prev_close,
            "prevCount": len(prevs),
            "candles": candles,
        }

    async def _positions(self) -> list[dict]:
        try:
            bal = await self._replay.get_balance()
        except Exception:  # noqa: BLE001
            return []
        # 종목별 신용(차입)액 — 브로커가 권위(요구 2: 신용/현금 구분 표시).
        broker_pos = {}
        try:
            _cash, broker_pos = self._broker.snapshot()
        except Exception:  # noqa: BLE001
            broker_pos = {}
        out: list[dict] = []
        for pos in bal.positions:
            if pos.qty <= 0:
                continue
            try:
                pct = float(pos.pnlPct)
            except (TypeError, ValueError):
                pct = 0.0
            bp = broker_pos.get(pos.code)
            credit = int(getattr(bp, "credit", 0) or 0)
            cost = pos.avgPrice * pos.qty
            out.append({
                "code": pos.code,
                "name": self._name_map.get(pos.code, pos.name or ""),
                "qty": pos.qty,
                "entry": pos.avgPrice,
                "current": pos.currentPrice,
                "pnlPct": round(pct, 2),
                "pnl": pos.pnl,
                # 신용/현금 구분 (요구 2).
                "credit": credit,                          # 신용 차입액
                "cashAmt": max(0, int(cost) - credit),     # 자기 현금 투입액
                "isCredit": credit > 0,
            })
        return out

    def _cumulative(self) -> dict:
        days = len(self._day_results)
        total_pnl = sum(r["pnl"] for r in self._day_results)
        avg_daily = (sum(r["pnl_pct"] for r in self._day_results) / days) if days else 0.0
        win_days = sum(1 for r in self._day_results if r["pnl"] > 0)
        exits = self._all_exit_pnls
        n = len(exits)
        wins = [e for e in exits if e > 0]
        losses = [e for e in exits if e < 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
            float("inf") if gross_win > 0 else 0.0
        )
        payoff = (
            (sum(wins) / len(wins)) / (abs(sum(losses)) / len(losses))
            if wins and losses else 0.0
        )
        # 최대 낙폭(MDD): 일별 손익 누적(자산곡선)의 고점 대비 최대 하락.
        equity = self._start_cash
        peak = self._start_cash
        mdd = 0.0
        for r in self._day_results:
            equity += r["pnl"]
            peak = max(peak, equity)
            if peak > 0:
                mdd = max(mdd, (peak - equity) / peak)
        return {
            "days": days,
            "totalPnl": int(total_pnl),
            "totalReturnPct": round(total_pnl / self._start_cash * 100, 2)
            if self._start_cash else 0.0,
            "avgDailyPct": round(avg_daily, 2),
            "winDays": win_days,
            "dayWinRate": round(win_days / days * 100, 1) if days else 0.0,
            "trades": n,
            "tradeWinRate": round(len(wins) / n * 100, 1) if n else 0.0,
            "profitFactor": round(profit_factor, 2)
            if profit_factor != float("inf") else None,
            "payoff": round(payoff, 2),
            "mddPct": round(mdd * 100, 2),
        }

    def _report(self, cum: dict) -> dict:
        """완료 리포트 데이터 (날짜별 손익 차트 + 최고/최악 패턴 + 제안)."""
        best = worst = None
        if self._memory is not None:
            try:
                v = self._memory.view()
                best, worst = v.best_pattern(), v.worst_pattern()
            except Exception:  # noqa: BLE001
                pass
        return {
            "dailyPnl": [
                {"date": r["date"], "pnl": r["pnl"], "pnlPct": r["pnl_pct"]}
                for r in self._day_results[-60:]
            ],
            "bestPattern": best,
            "worstPattern": worst,
            "suggestions": [
                {"kind": p["kind"], "rationale": p["rationale"]}
                for p in self._proposals[-5:]
            ],
            "mddPct": cum["mddPct"],
            "totalReturnPct": cum["totalReturnPct"],
            "tradeWinRate": cum["tradeWinRate"],
            "profitFactor": cum["profitFactor"],
        }

    def _perf_score(self, cum: dict) -> int:
        """0~100 종합 성과 점수 (승률·손익비 기반, 쉬운 표시용)."""
        wr = (cum.get("tradeWinRate") or 0) / 100.0
        pf = cum.get("profitFactor")
        pf = 1.0 if pf is None else min(float(pf), 3.0)
        score = wr * 60 + (pf / 3.0) * 40
        return max(0, min(100, round(score)))

    async def snapshot(self) -> dict:
        now = self._clock.now()
        cum = self._cumulative()
        # 진행 표시(요구 3): "N일차 / 총M일". 무제한 모드는 totalDays=None(=N일차만 표시).
        cum["dayIndex"] = self._day_no
        cum["totalDays"] = self._total_days
        positions = await self._positions()
        balance = await self._balance()
        chart = await self._focus_chart(positions)
        best = worst = None
        if self._memory is not None:
            try:
                v = self._memory.view()
                best, worst = v.best_pattern(), v.worst_pattern()
            except Exception:  # noqa: BLE001
                pass
        agents = {
            "ceo": {
                "mode": self._mode,
                "proposals": len(self._proposals),
                "days": cum["days"],
            },
            "screening": {"count": len(self._candidates), "top": self._candidates[:8]},
            "market": self._market,
            "signal": self._last_signal,
            "risk": self._last_risk,
            "order": self._last_order,
            "learning": {
                "proposals": self._proposals[-5:],
                "bestPattern": best, "worstPattern": worst,
            },
            "meta": {
                "observation": self._observation,
                "perfScore": self._perf_score(cum),
                "winRate": cum["tradeWinRate"],
                "profitFactor": cum["profitFactor"],
                "payoff": cum["payoff"],
                "tokenCalls": (self._observation or {}).get("tokenCalls", 0),
            },
        }
        return {
            "ok": True,
            "running": self._running,
            # 실행 중인 백테스트 프로세스 PID — 서버(server.js)가 고아 프로세스를
            # 식별·종료해 "이미 실행 중" 오류를 자가 복구하는 데 사용한다(요구 1).
            "pid": os.getpid(),
            # 일시정지 여부(요구 1) — PAUSE 센티넬 존재로 판정. 토글 버튼 상태의 단일 출처.
            "paused": (self._state_path.parent / "BACKTEST_PAUSE").exists(),
            "updatedAt": now.isoformat(),
            "sim": {
                "date": self._day_date,
                "time": _hhmmss(now),
                "datetime": now.isoformat(),
            },
            "screening": self._candidates,
            "balance": balance,
            "chart": chart,
            "positions": positions,
            # 백테스트는 PaperBroker 즉시 체결이라 미체결이 없다 → 빈 목록(요구 6 탭 소스).
            "unfilled": [],
            "todayTrades": list(reversed(self._trades))[:50],
            # 누적 거래 내역(최신순, 날짜 포함) — 거래 탭에 전체 기간 날짜별로 표시(요구 2).
            "allTrades": list(reversed(self._all_trades))[:1000],
            "cumulative": cum,
            "agents": agents,
            "lastDays": self._day_results[-10:],
            "report": self._report(cum),
        }

    # ─────────────────────────── 기록 루프 ───────────────────────────

    def _write(self, data: dict) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._state_path)

    async def run(self, stop_event: asyncio.Event) -> None:
        """주기적 기록 루프. 핫 루프와 동시 구동되며 파일 I/O만 수행(무영향)."""
        self._running = True
        try:
            while not stop_event.is_set():
                try:
                    self._write(await self.snapshot())
                except Exception:  # noqa: BLE001
                    log.debug("dashboard write 실패", exc_info=True)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    continue
        finally:
            self._running = False
            try:
                self._write(await self.snapshot())   # 종료 스냅샷
            except Exception:  # noqa: BLE001
                pass
