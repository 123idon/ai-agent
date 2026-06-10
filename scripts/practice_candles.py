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
    ap.add_argument("--date")
    ap.add_argument("--symbol")
    args = ap.parse_args()
    try:
        if args.list:
            _emit(action_list())
        elif args.daily and args.symbol:
            _emit(action_daily(args.symbol))
        elif args.date and args.symbol:
            _emit(action_candles(args.date, args.symbol))
        else:
            _emit({"ok": False, "error": "--list / --daily+--symbol / --date+--symbol 필요"})
    except Exception as e:  # noqa: BLE001 — 어떤 경우에도 JSON 한 줄
        _emit({"ok": False, "error": f"실행 오류: {e}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
