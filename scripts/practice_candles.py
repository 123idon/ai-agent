"""연습모드(과거 날짜 매매 연습) 데이터 공급기 — 1단계: 재생 엔진/차트용.

HTS '연습모드' 탭이 쓰는 로컬 분봉(``data/candles/{YYYYMMDD}.parquet``, §18)을 JSON으로
돌려준다. 브라우저는 parquet 을 직접 못 읽으므로 traidair server.js 가 본 스크립트를
spawn 해(strategy_settings.py 와 동일 패턴) 결과를 중계한다.

**읽기 전용 ETL 뷰** — 라이브 매매(§15)·백테스트 엔진과 무관하며 파일을 수정하지 않는다.
룩어헤드 차단은 클라이언트(엔진)가 '현재 시점 이전 봉만 노출'로 담당하고, 여기서는 하루치
1분봉 전체를 시간 오름차순으로 그대로 제공한다(3·5분봉 합성은 클라이언트가 수행).

사용:
  python scripts/practice_candles.py --list                       # 날짜(243)·종목(44) 목록
  python scripts/practice_candles.py --date 20251128 --symbol 000270
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANDLES = ROOT / "data" / "candles"
UNIVERSE = ROOT / "config" / "universe.json"


def _emit(obj: dict) -> None:
    """단일 JSON 라인 출력(server.js 가 마지막 '{' 라인을 파싱)."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    print(json.dumps(obj, ensure_ascii=False))


def _date_files() -> list[Path]:
    if not CANDLES.is_dir():
        return []
    out = []
    for p in CANDLES.glob("*.parquet"):
        if p.stem.startswith("_"):
            continue
        if len(p.stem) == 8 and p.stem.isdigit():
            out.append(p)
    return sorted(out, key=lambda p: p.stem)


def _name_map() -> dict[str, str]:
    """universe.json 의 모든 {code,name} 항목을 code→name 으로 모은다."""
    names: dict[str, str] = {}
    try:
        data = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    except Exception:
        return names
    for val in (data.values() if isinstance(data, dict) else []):
        if isinstance(val, list):
            for it in val:
                if isinstance(it, dict) and it.get("code") and it.get("name"):
                    names[str(it["code"])] = str(it["name"])
    return names


def action_list() -> dict:
    import pandas as pd  # 지연 임포트(목록만 필요할 때 빠르게 실패하지 않도록)

    files = _date_files()
    if not files:
        return {"ok": False, "error": "data/candles 에 분봉 parquet 이 없습니다"}
    dates = [p.stem for p in files]
    # 종목 목록: 가장 최신 파일 기준(가장 완전) — 6자리 종목코드만(지수 ^KS11 등 제외).
    names = _name_map()
    symbols: list[dict] = []
    try:
        df = pd.read_parquet(files[-1], columns=["symbol"])
        codes = sorted({str(s) for s in df["symbol"].unique()
                        if str(s).isdigit() and len(str(s)) == 6})
        symbols = [{"code": c, "name": names.get(c, c)} for c in codes]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"종목 목록 읽기 실패: {e}"}
    return {
        "ok": True,
        "dates": dates,
        "symbols": symbols,
        "dateRange": [dates[0], dates[-1]],
        "dateCount": len(dates),
        "symbolCount": len(symbols),
    }


def _prev_closes(date: str, symbol: str, need: int = 80, max_days: int = 3) -> list[int]:
    """선택일 직전 거래일(들)의 1분봉 종가를 시간 오름차순으로 반환(이동평균 워밍업용).

    **로컬 ``data/candles`` 파일만** 사용(외부 호출 0). 선택일 이전 날짜 파일을 최신→과거로
    훑어 같은 종목 종가를 ``need``(MA60+여유=80) 개 이상 모일 때까지 모으되 ``max_days`` 로 제한.
    반환은 **시간순(가장 오래된 것 먼저, 선택일 직전 봉이 맨 뒤)** — 당일 종가 앞에 그대로 이어
    붙이면 연속 시계열이 된다. 미래 차단과 무관(전일=과거).
    """
    import pandas as pd

    files = _date_files()  # 날짜 오름차순
    stems = [p.stem for p in files]
    if date not in stems:
        return []
    idx = stems.index(date)
    days_chrono: list[list[int]] = []  # 최신 전일 → 과거 순으로 append
    j = idx - 1
    used = 0
    while j >= 0 and used < max_days:
        try:
            df = pd.read_parquet(files[j], columns=["symbol", "t", "c"])
        except Exception:  # noqa: BLE001
            j -= 1
            continue
        sub = df[df["symbol"].astype(str) == symbol].sort_values("t")
        if not sub.empty:
            days_chrono.append([int(c) for c in sub["c"].tolist()])
            used += 1
            if sum(len(d) for d in days_chrono) >= need:
                break
        j -= 1
    out: list[int] = []
    for day in reversed(days_chrono):  # 과거 → 직전 전일 순(시간 오름차순)
        out.extend(day)
    return out


def _load_sectors() -> dict[str, str]:
    """config/sectors.json 의 종목코드→섹터 매핑(없으면 빈 dict). 외부 호출 0."""
    try:
        data = json.loads((ROOT / "config" / "sectors.json").read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in (data.get("sectors") or {}).items()}
    except Exception:  # noqa: BLE001
        return {}


def _market_context(date: str) -> dict | None:
    """data/market_context/{D}.json (장전 시황 — 내용은 전부 D-1 이전 세션, 룩어헤드 없음)."""
    f = ROOT / "data" / "market_context" / f"{date}.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _day_aggregate(path) -> dict:
    """하루치 parquet → {종목코드: {amt, vol, close}}.

    amt(거래대금 근사)=Σ(분봉종가 × 분봉거래량), vol=Σ분봉거래량, close=그날 마지막 봉 종가.
    지수(^KS11 등)·6자리 아닌 심볼은 제외. **이미 저장된 1분봉만** 사용(외부 호출 0).
    """
    import pandas as pd

    df = pd.read_parquet(path, columns=["symbol", "t", "c", "v"])
    df["symbol"] = df["symbol"].astype(str)
    df = df[df["symbol"].map(lambda s: s.isdigit() and len(s) == 6)]
    if df.empty:
        return {}
    df = df.assign(amt=df["c"].astype("int64") * df["v"].astype("int64"))
    out: dict = {}
    for sym, sub in df.groupby("symbol"):
        sub = sub.sort_values("t")
        out[str(sym)] = {
            "amt": int(sub["amt"].sum()),
            "vol": int(sub["v"].sum()),
            "close": int(sub["c"].iloc[-1]),
        }
    return out


def action_prep(date: str) -> dict:
    """장전 후보(종목 선정) — 선택일 D 의 **직전 거래일 D-1 기준**으로만 산출.

    ⭐ 룩어헤드 차단: 선택일 D 의 parquet 은 **절대 읽지 않는다**. D-1(거래대금·등락 기준)과
    D-2(D-1 등락률 계산용 전전일 종가)만 사용하며, 시황은 data/market_context/{D}.json
    (내용이 전부 D-1 이전 세션이라 장전에 알 수 있는 값)을 그대로 싣는다.

    반환: rows(종목별 전일 거래대금/거래량/등락률/섹터, 거래대금 내림차순),
          sectors(섹터별 자금 집계 내림차순), market(전일 시황), prevDate(=D-1).
    """
    date = "".join(ch for ch in str(date) if ch.isdigit())
    files = _date_files()  # 날짜 오름차순
    stems = [p.stem for p in files]
    if date not in stems:
        return {"ok": False, "error": f"해당 날짜 데이터 없음: {date}"}
    idx = stems.index(date)
    if idx < 1:
        return {"ok": False, "error": "직전 거래일(D-1) 데이터가 없습니다(가장 과거 날짜)"}
    prev = stems[idx - 1]
    try:
        prev_agg = _day_aggregate(files[idx - 1])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"전일 집계 실패: {e}"}
    prev_prev_agg = {}
    if idx >= 2:
        try:
            prev_prev_agg = _day_aggregate(files[idx - 2])
        except Exception:  # noqa: BLE001 — 전전일 없으면 등락률만 미표시(치명 아님)
            prev_prev_agg = {}

    names = _name_map()
    sectors = _load_sectors()
    rows: list[dict] = []
    for code, a in prev_agg.items():
        pclose = a["close"]
        pp = prev_prev_agg.get(code)
        chg = None
        if pp and pp.get("close"):
            chg = round((pclose - pp["close"]) / pp["close"] * 100.0, 2)
        rows.append({
            "code": code,
            "name": names.get(code, code),
            "sector": sectors.get(code, ""),
            "amt": a["amt"],
            "amtEok": round(a["amt"] / 1e8, 1),
            "vol": a["vol"],
            "close": pclose,
            "changePct": chg,
        })
    rows.sort(key=lambda r: r["amt"], reverse=True)

    sec_map: dict[str, dict] = {}
    for r in rows:
        s = r["sector"] or "기타"
        e = sec_map.setdefault(s, {"sector": s, "amt": 0, "count": 0})
        e["amt"] += r["amt"]
        e["count"] += 1
    sec_rows = sorted(sec_map.values(), key=lambda x: x["amt"], reverse=True)
    for e in sec_rows:
        e["amtEok"] = round(e["amt"] / 1e8, 1)

    return {
        "ok": True,
        "date": date,
        "prevDate": prev,
        "rows": rows,
        "sectors": sec_rows,
        "market": _market_context(date),
        "symbolCount": len(rows),
    }


def action_candles(date: str, symbol: str) -> dict:
    import pandas as pd

    date = "".join(ch for ch in str(date) if ch.isdigit())
    symbol = str(symbol).strip()
    f = CANDLES / f"{date}.parquet"
    if not f.is_file():
        return {"ok": False, "error": f"해당 날짜 데이터 없음: {date}"}
    try:
        df = pd.read_parquet(f)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"parquet 읽기 실패: {e}"}
    sub = df[df["symbol"].astype(str) == symbol]
    if sub.empty:
        return {"ok": False, "error": f"{date} 에 {symbol} 분봉 없음"}
    sub = sub.sort_values("t")
    candles = [
        {
            "t": str(r.t),
            "o": int(r.o), "h": int(r.h), "l": int(r.l), "c": int(r.c),
            "v": int(r.v),
        }
        for r in sub.itertuples(index=False)
    ]
    # 이동평균이 09:00 첫 봉부터 이어지도록 직전 거래일 종가를 함께 제공(MA 계산용, 화면 미표시)
    prev_closes = _prev_closes(date, symbol)
    return {"ok": True, "date": date, "symbol": symbol,
            "count": len(candles), "candles": candles,
            "prevCloses": prev_closes, "prevCount": len(prev_closes)}


def action_daily(symbol: str) -> dict:
    """저장된 1분봉(data/candles/*.parquet)을 날짜별로 묶어 일봉으로 합성한다.

    시가=그날 첫 봉 시가 / 고가=그날 최고 / 저가=그날 최저 / 종가=그날 마지막 봉 종가 /
    거래량=그날 합. KIS·외부 API 호출 없이 **이미 저장된 1분봉만** 사용한다(§18). 재생용이
    아니라 종목의 전체 기간(거래일 전체) 추세를 한 화면에 보여주는 참고용 큰 그림이다.
    날짜 오름차순으로 거래일 수만큼(예: ~245일) 일봉을 돌려준다.
    """
    import pandas as pd

    symbol = str(symbol).strip()
    files = _date_files()  # 날짜 오름차순
    if not files:
        return {"ok": False, "error": "data/candles 에 분봉 parquet 이 없습니다"}
    candles: list[dict] = []
    for f in files:
        try:
            df = pd.read_parquet(f, columns=["symbol", "t", "o", "h", "l", "c", "v"])
        except Exception:
            continue  # 손상/누락 날짜는 건너뛴다(전체 합성 중단 없음)
        sub = df[df["symbol"].astype(str) == symbol]
        if sub.empty:
            continue
        sub = sub.sort_values("t")
        candles.append({
            "t": f.stem, "date": f.stem,
            "o": int(sub.iloc[0].o),
            "h": int(sub["h"].max()),
            "l": int(sub["l"].min()),
            "c": int(sub.iloc[-1].c),
            "v": int(sub["v"].sum()),
        })
    if not candles:
        return {"ok": False, "error": f"{symbol} 일봉 합성 데이터 없음"}
    return {"ok": True, "symbol": symbol, "count": len(candles), "candles": candles}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--daily", action="store_true")
    ap.add_argument("--prep", action="store_true")
    ap.add_argument("--date")
    ap.add_argument("--symbol")
    args = ap.parse_args()
    try:
        if args.list:
            _emit(action_list())
        elif args.prep and args.date:
            _emit(action_prep(args.date))
        elif args.daily and args.symbol:
            _emit(action_daily(args.symbol))
        elif args.date and args.symbol:
            _emit(action_candles(args.date, args.symbol))
        else:
            _emit({"ok": False, "error": "--list / --prep+--date / --daily+--symbol / --date+--symbol 필요"})
    except Exception as e:  # noqa: BLE001 — 어떤 경우에도 JSON 한 줄
        _emit({"ok": False, "error": f"실행 오류: {e}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
