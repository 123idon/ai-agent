# -*- coding: utf-8 -*-
"""KRX 공매도·수급 일별 수집기 (pykrx, .venv-collect 전용).

연습 기간(기본 2025-06-02 ~ 2026-06-04)의 44종목에 대해 일별로 수집해
``data/krx_daily/{YYYYMMDD}.json`` (시황 market_context 와 동일한 날짜별 파일)로 저장한다.

수집 항목(종목별):
  - 공매도 거래량/비중 : get_shorting_volume_by_date  → 공매도, 매수(전체거래량), 비중
  - 공매도 잔고/금액/비중: get_shorting_balance_by_date → 공매도잔고, 상장주식수, 공매도금액, 시가총액, 비중
  - 수급 순매수(거래대금): get_market_trading_value_by_date → 기관/개인/외국인/기타법인

호출 절약:
  - 종목당 함수 3개를 '기간 전체' 한 번씩만 호출(종목별 3콜) → 44종목 = 132콜.
  - 종목 간 텀(--throttle, 기본 1.0s). KRX 무분별 호출 자제.
  - 날짜별 파일은 종목 단위로 '병합'(기존 종목 보존) → 샘플 후 전체 이어받기 가능.

로그인: config/kis_api.yaml 의 krx_id/krx_pw 만 읽어 KRX_ID/KRX_PW env 설정(다른 키 불변).
  pykrx 가 세션 만료 시 자동 재로그인(get_auth_session). 저장 데이터에 계정/비밀번호 미포함.

사용:
  # 샘플(형식 확인): 3종목 × 3일
  python scripts/collect_krx.py --start 2026-06-02 --end 2026-06-04 --symbols 005930,000660,035420
  # 전체
  python scripts/collect_krx.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "krx_daily"
CANDLES = ROOT / "data" / "candles"
YAML = ROOT / "config" / "kis_api.yaml"

DEFAULT_START = "2025-06-02"
DEFAULT_END = "2026-06-04"


def _log(msg: str) -> None:
    print(msg, flush=True)


def read_krx_creds() -> tuple[str | None, str | None]:
    """yaml 에서 krx_id/krx_pw 만 추출(다른 키 무시·미수정). 값은 로그에 미출력."""
    raw = YAML.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        cfg = raw.decode("utf-16")
    elif raw[:3] == b"\xef\xbb\xbf":
        cfg = raw.decode("utf-8-sig")
    else:
        cfg = raw.decode("utf-8")

    def grab(key: str) -> str | None:
        m = re.search(rf"^{re.escape(key)}\s*:\s*(.+?)\s*$", cfg, re.M)
        if not m:
            return None
        v = m.group(1).rstrip()
        if v and v[0] in "'\"":
            q = v[0]
            j = v.find(q, 1)
            return v[1:j] if j > 0 else v[1:]
        return (v.split("#")[0].strip() or None)

    return grab("krx_id"), grab("krx_pw")


def candle_symbols() -> list[str]:
    """data/candles 전체 parquet 의 6자리 종목코드 합집합(연습 44종목)."""
    import pandas as pd

    allc: set[str] = set()
    for f in sorted(CANDLES.glob("[0-9]*.parquet")):
        try:
            df = pd.read_parquet(f, columns=["symbol"])
        except Exception:
            continue
        allc |= {
            str(s) for s in df["symbol"].unique()
            if str(s).isdigit() and len(str(s)) == 6
        }
    return sorted(allc)


def _f(v) -> float | None:
    """비중(%) 등 실수값. float32 잡음 제거 위해 2자리 반올림."""
    try:
        import math
        x = float(v)
        return None if math.isnan(x) else round(x, 2)
    except Exception:
        return None


def _i(v) -> int | None:
    x = _f(v)
    return None if x is None else int(round(x))


def collect_symbol(stock, code: str, frm: str, to: str) -> dict[str, dict]:
    """한 종목의 기간 전체를 조회 → {YYYYMMDD: record}."""
    per_date: dict[str, dict] = {}

    def ensure(d: str) -> dict:
        return per_date.setdefault(d, {"short": {}, "flow": {}})

    # 1) 공매도 거래량/비중
    try:
        df = stock.get_shorting_volume_by_date(frm, to, code)
        for idx, row in df.iterrows():
            d = idx.strftime("%Y%m%d")
            rec = ensure(d)["short"]
            rec["volume"] = _i(row.get("공매도"))          # 공매도 거래량
            rec["total_volume"] = _i(row.get("매수"))       # 전체 거래량
            rec["volume_ratio"] = _f(row.get("비중"))       # 공매도 비중 %
    except Exception as e:
        _log(f"    [warn] {code} shorting_volume: {type(e).__name__}: {e}")

    # 2) 공매도 잔고/금액/비중
    try:
        df = stock.get_shorting_balance_by_date(frm, to, code)
        for idx, row in df.iterrows():
            d = idx.strftime("%Y%m%d")
            rec = ensure(d)["short"]
            rec["balance_qty"] = _i(row.get("공매도잔고"))
            rec["listed_shares"] = _i(row.get("상장주식수"))
            rec["balance_amount"] = _i(row.get("공매도금액"))
            rec["market_cap"] = _i(row.get("시가총액"))
            rec["balance_ratio"] = _f(row.get("비중"))       # 잔고 비중 %
    except Exception as e:
        _log(f"    [warn] {code} shorting_balance: {type(e).__name__}: {e}")

    # 3) 수급 순매수 거래대금(원)
    try:
        df = stock.get_market_trading_value_by_date(frm, to, code)
        for idx, row in df.iterrows():
            d = idx.strftime("%Y%m%d")
            rec = ensure(d)["flow"]
            rec["inst"] = _i(row.get("기관합계"))            # 기관 순매수
            rec["retail"] = _i(row.get("개인"))              # 개인 순매수
            rec["foreign"] = _i(row.get("외국인합계"))        # 외국인 순매수
            rec["etc_corp"] = _i(row.get("기타법인"))         # 기타법인
    except Exception as e:
        _log(f"    [warn] {code} trading_value: {type(e).__name__}: {e}")

    return per_date


def write_day(date: str, symbols: dict[str, dict]) -> None:
    """날짜별 파일에 종목 단위 병합 저장(기존 종목 보존)."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{date}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    else:
        existing = {}
    syms = existing.get("symbols", {})
    syms.update(symbols)
    out = {
        "date": date,
        "tradingDay": f"{date[:4]}-{date[4:6]}-{date[6:]}",
        "source": "pykrx/KRX",
        "fields": {
            "short": ["volume", "total_volume", "volume_ratio",
                      "balance_qty", "listed_shares", "balance_amount",
                      "market_cap", "balance_ratio"],
            "flow": ["inst", "retail", "foreign", "etc_corp"],
        },
        "note": "short=공매도(거래량/비중·잔고/금액/비중) · flow=투자자별 순매수 거래대금(원)",
        "symbols": syms,
    }
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--symbols", default="", help="쉼표구분 코드(미지정=연습 44종목 전체)")
    ap.add_argument("--throttle", type=float, default=1.0, help="종목 간 텀(초)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    # --- 로그인 자격: yaml → env ---
    kid, kpw = read_krx_creds()
    if not (kid and kpw):
        _log("[fatal] config/kis_api.yaml 에서 krx_id/krx_pw 를 읽지 못했습니다.")
        return 2
    os.environ["KRX_ID"] = kid
    os.environ["KRX_PW"] = kpw
    _log(f"[login] KRX_ID={kid} (pw 미출력) — pykrx 자동 로그인/재로그인")

    from pykrx import stock  # import 시 자동 로그인

    symbols = (
        [s.strip() for s in args.symbols.split(",") if s.strip()]
        if args.symbols else candle_symbols()
    )
    frm = args.start.replace("-", "")
    to = args.end.replace("-", "")
    _log(f"[collect] {len(symbols)}종목 · {frm}~{to} · throttle={args.throttle}s")

    by_date: dict[str, dict[str, dict]] = {}
    for i, code in enumerate(symbols, 1):
        _log(f"  [{i}/{len(symbols)}] {code} ...")
        per_date = collect_symbol(stock, code, frm, to)
        for d, rec in per_date.items():
            by_date.setdefault(d, {})[code] = rec
        if i < len(symbols) and args.throttle > 0:
            time.sleep(args.throttle)

    for d in sorted(by_date):
        write_day(d, by_date[d])
    _log(f"[done] {len(by_date)}일 저장 → {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
