"""core.memory — 저널 집계 / 에이전트 질의 (CLAUDE.md §19)."""
from __future__ import annotations

import json
from pathlib import Path

from core.memory import MemoryStore, MemoryView, indicator_label, pattern_key


def _journal(tmp_path: Path, date: str, records: list[dict]) -> Path:
    jd = tmp_path / "data" / "journal"
    jd.mkdir(parents=True, exist_ok=True)
    with (jd / f"{date}.jsonl").open("w", encoding="utf-8") as f:
        for topic, payload in records:
            f.write(json.dumps({"topic": topic, "ts": date, "payload": payload},
                               ensure_ascii=False) + "\n")
    return jd


def _round(symbol: str, passed: list[str], signal: str, pnl: float, kind: str) -> list:
    return [
        ("signal.analysis", {"symbol": symbol, "signal": signal,
                             "indicators": [{"name": n, "passed": n in passed}
                                            for n in ["volume", "rsi", "macd", "ma", "candle"]]}),
        ("order.event", {"side": "buy", "symbol": symbol, "qty": 10, "price": 100}),
        ("signal.exit", {"symbol": symbol, "pnl_pct": pnl, "kind": kind}),
    ]


def test_rebuild_aggregates_symbol_pattern_grade(tmp_path) -> None:
    recs = [("market.state", {"grade": "GREEN"})]
    recs += _round("005930", ["volume", "rsi", "macd", "ma"], "STRONG_ENTRY", 0.04, "take_profit_1")
    recs += _round("005930", ["volume", "rsi", "macd", "ma"], "STRONG_ENTRY", -0.03, "hard_stop_loss")
    recs += _round("005930", ["volume", "rsi", "macd", "ma"], "STRONG_ENTRY", -0.02, "technical_stop")
    _journal(tmp_path, "20240104", recs)

    store = MemoryStore(tmp_path)
    store.rebuild(tmp_path / "data" / "journal")

    s = store.symbol["005930"]
    assert s["trades"] == 3 and s["wins"] == 1 and s["stoploss"] == 2
    assert s["winRate"] == 33.3

    pk = pattern_key("STRONG_ENTRY", ["volume", "rsi", "macd", "ma"])
    p = store.pattern[pk]
    assert p["trades"] == 3 and p["wins"] == 1
    assert "강한진입" in p["label"]

    assert store.grade["GREEN"]["trades"] == 3

    # 영속화 확인
    assert (tmp_path / "data" / "memory" / "symbol_stats.json").exists()
    reloaded = MemoryStore(tmp_path)
    assert reloaded.symbol["005930"]["trades"] == 3


def test_view_queries() -> None:
    v = MemoryView(
        symbol={"A": {"trades": 4, "wins": 1, "stoploss": 3, "winRate": 25.0}},
        pattern={
            "STRONG_ENTRY|ma+rsi": {"label": "강한진입 · RSI+이평", "trades": 6, "wins": 5, "winRate": 83.3},
            "CONDITIONAL_ENTRY|volume": {"label": "조건부진입 · 거래량", "trades": 6, "wins": 1, "winRate": 16.7},
        },
        grade={"RED": {"trades": 8, "wins": 1, "winRate": 12.5}},
    )
    # 반복 손절 종목 → 음수 가점
    assert v.symbol_score_adjust("A") < 0
    assert v.symbol_score_adjust("ZZZ") == 0.0
    # 패턴 신뢰도 (표본 충분)
    wr, n = v.pattern_confidence("CONDITIONAL_ENTRY", ["volume"])
    assert wr == 16.7 and n == 6
    # 표본 부족 → None
    assert v.pattern_confidence("STRONG_ENTRY", ["macd"]) == (None, 0)
    # 시장 등급 승률
    assert v.grade_winrate("RED") == (12.5, 8)
    # best/worst
    assert v.best_pattern()["winRate"] == 83.3
    assert v.worst_pattern()["winRate"] == 16.7


def test_labels() -> None:
    assert indicator_label("STRONG_ENTRY", ["volume", "rsi"]) == "강한진입 · RSI+거래량"
    assert pattern_key("X", ["b", "a"]) == "X|a+b"
