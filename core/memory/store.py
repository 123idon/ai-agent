"""장기 메모리 저장소 (CLAUDE.md §19).

저널 envelope({topic, ts, payload})을 시간순으로 훑어 집계한다. 단일 집중 운영
(§5.7)이라 한 시점에 한 종목만 보유하므로, ``order.event(buy)`` 직전 ``signal.analysis``
로 진입 패턴을, 직전 ``market.state``로 시장 등급을 묶고, 뒤따르는 ``signal.exit``의
손익으로 결과(승/패)를 귀속한다.

산출:
- symbol_stats: 종목별 {trades, wins, stoploss, winRate, lastDates}
- pattern_stats: 패턴별 {label, trades, wins, winRate}  (패턴 = 신호강도 + 통과지표 조합)
- grade_stats:  시장등급별 {trades, wins, winRate}

모두 ``data/memory/*.json``에 저장하고, ``MemoryView``가 에이전트용 질의를 제공한다.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_STOPLOSS_KINDS = {"hard_stop_loss", "technical_stop", "signal_breakdown"}
_IND_KO = {"volume": "거래량", "rsi": "RSI", "macd": "MACD", "ma": "이평", "candle": "캔들"}
_SIG_KO = {"STRONG_ENTRY": "강한진입", "CONDITIONAL_ENTRY": "조건부진입", "NO_ENTRY": "미진입"}


def pattern_key(signal: str, passed: list[str]) -> str:
    return f"{signal}|{'+'.join(sorted(passed))}"


def indicator_label(signal: str, passed: list[str]) -> str:
    inds = "+".join(_IND_KO.get(p, p) for p in sorted(passed)) or "지표없음"
    return f"{_SIG_KO.get(signal, signal)} · {inds}"


def _rate(wins: int, trades: int) -> float:
    return round(wins / trades * 100, 1) if trades else 0.0


class MemoryView:
    """집계 결과에 대한 에이전트용 읽기 인터페이스."""

    def __init__(
        self, symbol: dict, pattern: dict, grade: dict, *,
        stoploss_floor: int = 3, low_winrate: float = 40.0, min_trades: int = 4,
    ) -> None:
        self.symbol = symbol
        self.pattern = pattern
        self.grade = grade
        self._sl_floor = stoploss_floor
        self._low = low_winrate
        self._min = min_trades

    def symbol_score_adjust(self, code: str) -> float:
        """반복 손절 종목에 음수 가점(기준 강화). 0 이하."""
        s = self.symbol.get(code)
        if not s:
            return 0.0
        sl = int(s.get("stoploss", 0))
        if sl >= self._sl_floor and s.get("winRate", 100) < self._low:
            # 보수적 넛지: 순위를 낮추되 유니버스를 통째로 비우지 않도록 소폭만 감점.
            return -min(sl, 3) * 4.0   # 최대 -12점
        return 0.0

    def symbol_note(self, code: str) -> str:
        s = self.symbol.get(code)
        if not s or not s.get("trades"):
            return ""
        return (f"최근 {s['trades']}회 중 손절 {s.get('stoploss', 0)}회, "
                f"승률 {s.get('winRate', 0)}%")

    def pattern_confidence(self, signal: str, passed: list[str]) -> tuple[float | None, int]:
        p = self.pattern.get(pattern_key(signal, passed))
        if not p or p.get("trades", 0) < self._min:
            return None, (p.get("trades", 0) if p else 0)
        return float(p["winRate"]), int(p["trades"])

    def grade_winrate(self, grade: str) -> tuple[float | None, int]:
        g = self.grade.get(grade)
        if not g or g.get("trades", 0) < self._min:
            return None, (g.get("trades", 0) if g else 0)
        return float(g["winRate"]), int(g["trades"])

    def best_pattern(self) -> dict | None:
        cands = [p for p in self.pattern.values() if p.get("trades", 0) >= self._min]
        return max(cands, key=lambda p: p["winRate"], default=None)

    def worst_pattern(self) -> dict | None:
        cands = [p for p in self.pattern.values() if p.get("trades", 0) >= self._min]
        return min(cands, key=lambda p: p["winRate"], default=None)


class MemoryStore:
    def __init__(self, root: Path, *, lookback_days: int = 20) -> None:
        self.dir = Path(root) / "data" / "memory"
        self._lookback = lookback_days
        self.symbol: dict[str, dict] = {}
        self.pattern: dict[str, dict] = {}
        self.grade: dict[str, dict] = {}
        self.load()

    # ─────────────────────────── 영속화 ───────────────────────────

    def load(self) -> None:
        for name, attr in (("symbol_stats", "symbol"), ("pattern_stats", "pattern"),
                           ("grade_stats", "grade")):
            p = self.dir / f"{name}.json"
            if p.exists():
                try:
                    setattr(self, attr, json.loads(p.read_text(encoding="utf-8")))
                except (json.JSONDecodeError, OSError):
                    pass

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        for name, data in (("symbol_stats", self.symbol), ("pattern_stats", self.pattern),
                           ("grade_stats", self.grade)):
            p = self.dir / f"{name}.json"
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, p)
        view = self.view()
        summary = {
            "patterns": len(self.pattern), "symbols": len(self.symbol),
            "bestPattern": view.best_pattern(), "worstPattern": view.worst_pattern(),
            "gradeStats": self.grade,
        }
        (self.dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    def view(self) -> MemoryView:
        return MemoryView(self.symbol, self.pattern, self.grade)

    # ─────────────────────────── 집계 ───────────────────────────

    def rebuild(self, journal_dir: Path) -> MemoryView:
        """최근 lookback_days 저널을 재집계해 메모리를 갱신·저장한다."""
        jd = Path(journal_dir)
        files = sorted(jd.glob("*.jsonl"))[-self._lookback:] if jd.exists() else []
        symbol: dict[str, dict] = {}
        pattern: dict[str, dict] = {}
        grade: dict[str, dict] = {}

        cur_grade = "GREEN"
        last_analysis: dict[str, dict] = {}
        open_ctx: dict[str, dict] = {}

        def _bump(d: dict, key: str, win: bool, *, label: str | None = None,
                  stoploss: bool = False, date: str | None = None) -> None:
            e = d.setdefault(key, {"trades": 0, "wins": 0, "stoploss": 0,
                                   "winRate": 0.0, "lastDates": []})
            e["trades"] += 1
            e["wins"] += 1 if win else 0
            if stoploss:
                e["stoploss"] += 1
            if label:
                e["label"] = label
            if date:
                e["lastDates"] = (e["lastDates"] + [date])[-5:]
            e["winRate"] = _rate(e["wins"], e["trades"])

        for fp in files:
            fdate = fp.stem
            try:
                lines = fp.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                topic = rec.get("topic", "")
                p = rec.get("payload") or {}
                if not isinstance(p, dict):
                    continue
                if topic == "market.state":
                    cur_grade = p.get("grade", cur_grade)
                elif topic == "signal.analysis":
                    sym = p.get("symbol", "")
                    passed = [i.get("name") for i in (p.get("indicators") or [])
                              if i.get("passed")]
                    last_analysis[sym] = {"signal": p.get("signal", ""), "passed": passed}
                elif topic == "order.event" and p.get("side") == "buy":
                    sym = p.get("symbol", "")
                    a = last_analysis.get(sym, {"signal": "", "passed": []})
                    open_ctx[sym] = {
                        "pattern": pattern_key(a["signal"], a["passed"]),
                        "label": indicator_label(a["signal"], a["passed"]),
                        "grade": cur_grade, "date": fdate,
                    }
                elif topic == "signal.exit":
                    sym = p.get("symbol", "")
                    win = float(p.get("pnl_pct", 0.0)) > 0
                    sl = p.get("kind", "") in _STOPLOSS_KINDS
                    _bump(symbol, sym, win, stoploss=sl, date=fdate)
                    ctx = open_ctx.pop(sym, None)
                    if ctx:
                        _bump(pattern, ctx["pattern"], win, label=ctx["label"])
                        _bump(grade, ctx["grade"], win)

        self.symbol, self.pattern, self.grade = symbol, pattern, grade
        self._save()
        log.info("MEMORY 갱신: 종목 %d, 패턴 %d, 등급 %d",
                 len(symbol), len(pattern), len(grade))
        return self.view()
