"""장 전 맥락(지수) 수집 — 연습모드용 그날 고정 시황 데이터.

연습 기간의 각 **한국 거래일 D** 에 대해, 그날 장 시작 전 알 수 있는 지수 맥락을
``data/market_context/{YYYYMMDD}.json`` 으로 날짜별 저장한다(로컬 재사용 — 연습 시 매번
외부 호출하지 않음). 외부 API(yfinance/Yahoo)는 **수집 시 1회만** 호출한다.

매핑(룩어헤드 없음 — 전부 D 이전 세션):
  · 나스닥종합(^IXIC) = D **직전 미국 거래일** 종가(= D 새벽에 끝난 미국장, "전일밤 미국증시")
  · 코스피(^KS11)/코스닥(^KQ11) = D **직전 한국 거래일** 종가(= "전일 지수")

KR 거래일 = ``data/candles`` 에 분봉이 있는 날짜(실제 연습 가능일).

실행(반드시 수집기 전용 venv):
  .venv-collect/Scripts/python.exe scripts/collect_market_context.py --days 3      # 샘플
  .venv-collect/Scripts/python.exe scripts/collect_market_context.py               # 전체 기간
옵션: --start/--end(YYYY-MM-DD), --days N(최근 N일만), --overwrite(기존 날짜도 덮어씀)
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANDLES = ROOT / "data" / "candles"
OUTDIR = ROOT / "data" / "market_context"

TICKERS = {
    "nasdaq": ("^IXIC", "나스닥종합", "직전 미국장 마감(전일밤)"),
    "kospi": ("^KS11", "코스피", "직전 한국 거래일 종가"),
    "kosdaq": ("^KQ11", "코스닥", "직전 한국 거래일 종가"),
}


def _kr_trading_days(start: str, end: str) -> list[str]:
    """data/candles 에 분봉이 있는 날짜(YYYYMMDD) 중 [start,end] 범위, 오름차순."""
    s = start.replace("-", "")
    e = end.replace("-", "")
    out = []
    if CANDLES.is_dir():
        for p in CANDLES.glob("*.parquet"):
            st = p.stem
            if len(st) == 8 and st.isdigit() and s <= st <= e:
                out.append(st)
    return sorted(out)


def _series(ticker: str, start: str, end: str):
    """yfinance 일별 종가 시리즈 → {"YYYY-MM-DD": (close, changePct)} (오름차순 키 리스트도 반환)."""
    import yfinance as yf
    import pandas as pd

    df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
    if df is None or df.empty:
        return {}, []
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close[ticker] if ticker in close.columns else close.iloc[:, 0]
    close = close.dropna()
    pct = close.pct_change() * 100.0
    data = {}
    for ts, c in close.items():
        ds = str(ts)[:10]
        p = pct.loc[ts]
        data[ds] = (round(float(c), 2), (None if p != p else round(float(p), 2)))
    return data, sorted(data.keys())


def _last_before(dates: list[str], d_iso: str):
    """정렬된 날짜 리스트에서 d_iso(YYYY-MM-DD) 보다 **작은** 마지막 날짜. 없으면 None."""
    prev = None
    for ds in dates:
        if ds < d_iso:
            prev = ds
        else:
            break
    return prev


def build(start: str, end: str, days: int | None, overwrite: bool) -> dict:
    kr_days = _kr_trading_days(start, end)
    if days:
        kr_days = kr_days[-days:]  # 최근 N일(샘플)
    if not kr_days:
        return {"ok": False, "error": "data/candles 에 해당 범위 거래일이 없습니다"}

    # 버퍼 포함 fetch(첫 거래일의 '직전 세션'까지 확보 + pct_change 용 그 이전 1세션)
    buf_start = (datetime.strptime(start, "%Y-%m-%d")).replace(day=1).strftime("%Y-%m-%d")
    # 한 달 더 앞으로
    by = int(buf_start[:4]); bm = int(buf_start[5:7])
    bm -= 1
    if bm == 0:
        bm = 12; by -= 1
    buf_start = f"{by:04d}-{bm:02d}-01"
    buf_end = (datetime.strptime(end, "%Y-%m-%d")).strftime("%Y-%m-%d")
    # yfinance end 는 배타적 → 하루 여유
    series = {}
    for key, (tk, _nm, _note) in TICKERS.items():
        data, keys = _series(tk, buf_start, buf_end)
        series[key] = (data, keys)
        print(f"  fetch {tk:7s} {len(keys)}일 ({keys[0] if keys else '-'} ~ {keys[-1] if keys else '-'})")

    OUTDIR.mkdir(parents=True, exist_ok=True)
    written, skipped = [], []
    for d in kr_days:
        f = OUTDIR / f"{d}.json"
        if f.exists() and not overwrite:
            skipped.append(d)
            continue
        d_iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        ctx = {"date": d, "tradingDay": d_iso, "source": "yfinance"}
        for key, (tk, nm, note) in TICKERS.items():
            data, keys = series[key]
            sess = _last_before(keys, d_iso)
            if sess is None:
                ctx[key] = {"ticker": tk, "name": nm, "sessionDate": None,
                            "close": None, "changePct": None, "note": note}
            else:
                c, p = data[sess]
                ctx[key] = {"ticker": tk, "name": nm, "sessionDate": sess,
                            "close": c, "changePct": p, "note": note}
        f.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(d)
    return {"ok": True, "written": written, "skipped": skipped,
            "outdir": str(OUTDIR), "krDays": len(kr_days)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-06-02")
    ap.add_argument("--end", default="2026-06-04")
    ap.add_argument("--days", type=int, default=None, help="최근 N일만(샘플)")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    res = build(args.start, args.end, args.days, args.overwrite)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
