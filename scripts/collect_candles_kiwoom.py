"""키움 REST API 과거 분봉 대량 수집기 (CLAUDE.md §18.1).

기존 KIS/Yahoo 경로(§15/§18)는 그대로 두고, **키움 REST API 전용** 수집 경로로
**2023-01-01 ~ 현재 전체 기간**의 분봉을 받아 ``C:\\ai-team\\data\\candles\\{YYYYMMDD}.parquet``
에 날짜별로 저장한다(종목은 한 날짜 파일 안에 병합 누적). 백테스트(§17)의 데이터 소스다.

설계 요지 — **대량/장시간 + 안전 + 재개 가능**:
- 키움 ka10080(주식분봉차트)은 날짜 파라미터가 없고 최신→과거로 페이지네이션한다.
  종목마다 ``--start`` 경계(2023-01-01)에 닿을 때까지 과거로 넘긴다(``cont-yn``/``next-key``).
- **레이트리밋 안전**: 페이지 간/종목 간 throttle + 429/5xx 지수 백오프(``KiwoomClient``).
- **진행률(%) + ETA** 표시.
- **재개 가능**: 완료 종목을 ``data/candles/_kiwoom_progress.json`` 에 체크포인트.
  중간에 끊겨도(Ctrl+C 포함) 다음 실행 시 **남은 종목만** 이어서 받는다. 부분 배치는
  flush 시점에 날짜 파일로 **병합**(``CandleStore.merge_day``)되므로 이미 받은 데이터는 보존.
- 키 우선순위: ``config/kis_api.yaml`` ``kiwoom_app_key/secret`` → 루트 ``*_appkey.txt``/
  ``*_secretkey.txt`` → env ``KIWOOM_APP_KEY``/``KIWOOM_APP_SECRET``.

사용:
  python scripts/collect_candles_kiwoom.py                       # 2023-01-01~오늘, 1분봉
  python scripts/collect_candles_kiwoom.py --start 2024-01-01
  python scripts/collect_candles_kiwoom.py --interval 5m
  python scripts/collect_candles_kiwoom.py --reset               # 체크포인트 초기화(처음부터)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))

from core.marketdata import CandleStore, KiwoomAuthError, KiwoomClient, load_universe

log = logging.getLogger("collect_candles_kiwoom")

_PROGRESS_NAME = "_kiwoom_progress.json"


# ─────────────────────────── 키/유니버스 ───────────────────────────

def _first_nonempty(*vals: object) -> str:
    for v in vals:
        if v and str(v).strip():
            return str(v).strip()
    return ""


def _read_key_file(root: Path, suffix: str) -> str:
    # key/ 폴더 우선, 없으면 루트 폴백(하위호환).
    for d in (root / "key", root):
        for p in sorted(d.glob(f"*_{suffix}.txt")):
            try:
                txt = p.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if txt:
                return txt
    return ""


def load_kiwoom_keys(root: Path) -> tuple[str, str, str]:
    """(app_key, app_secret, base_url) — config → 키파일 → env 순."""
    cfg: dict = {}
    # 키/비밀은 key/ 폴더 우선, 없으면 기존 config/ 폴백(하위호환).
    kis_path = root / "key" / "kis_api.yaml"
    if not kis_path.exists():
        kis_path = root / "config" / "kis_api.yaml"
    if kis_path.exists():
        try:
            cfg = yaml.safe_load(kis_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            cfg = {}
    app_key = _first_nonempty(
        cfg.get("kiwoom_app_key"), _read_key_file(root, "appkey"),
        os.getenv("KIWOOM_APP_KEY"),
    )
    app_secret = _first_nonempty(
        cfg.get("kiwoom_app_secret"), _read_key_file(root, "secretkey"),
        os.getenv("KIWOOM_APP_SECRET"),
    )
    base_url = _first_nonempty(
        cfg.get("kiwoom_base_url"), os.getenv("KIWOOM_BASE_URL"),
        "https://api.kiwoom.com",
    )
    return app_key, app_secret, base_url


def _stock_codes(univ: dict) -> list[tuple[str, str]]:
    """(code, name) — 코스피200 + 코스닥150 (지수 제외, 키움은 6자리 코드)."""
    out: list[tuple[str, str]] = []
    for s in univ.get("kospi200", []) + univ.get("kosdaq150", []):
        code = str(s.get("code", "")).strip()
        if code:
            out.append((code, s.get("name", "")))
    return out


# ─────────────────────────── 체크포인트 ───────────────────────────

def _progress_path(store: CandleStore) -> Path:
    return store.dir / _PROGRESS_NAME


def _load_progress(store: CandleStore) -> dict:
    p = _progress_path(store)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"done": [], "interval": "", "start": ""}


def _save_progress(store: CandleStore, prog: dict) -> None:
    p = _progress_path(store)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(prog, ensure_ascii=False, indent=0), encoding="utf-8")
    os.replace(tmp, p)


def _fmt_eta(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}시간 {m}분"
    if m:
        return f"{m}분 {s}초"
    return f"{s}초"


# ─────────────────────────── 수집 ───────────────────────────

async def collect(
    *, start: date, end: date, interval: str, max_pages: int,
    page_throttle: float, symbol_throttle: float, batch: int,
    root: Path, reset: bool,
) -> None:
    app_key, app_secret, base_url = load_kiwoom_keys(root)
    if not app_key or not app_secret:
        raise KiwoomAuthError(
            "키움 키를 찾지 못했습니다 — config/kis_api.yaml 의 kiwoom_app_key/"
            "kiwoom_app_secret, 또는 루트 *_appkey.txt/*_secretkey.txt, 또는 "
            "env KIWOOM_APP_KEY/KIWOOM_APP_SECRET 중 하나를 설정하세요."
        )

    univ = load_universe(root / "config" / "universe.json")
    codes = _stock_codes(univ)
    store = CandleStore(root)
    start_ymd = start.strftime("%Y%m%d")
    end_ymd = end.strftime("%Y%m%d")

    prog = _load_progress(store)
    # interval/start 가 바뀌면 체크포인트 무효(다른 수집 작업) → 초기화.
    if reset or prog.get("interval") not in ("", interval) or prog.get("start") not in ("", start_ymd):
        prog = {"done": [], "interval": interval, "start": start_ymd}
    prog["interval"] = interval
    prog["start"] = start_ymd
    done = set(prog.get("done", []))

    pending = [(c, n) for (c, n) in codes if c not in done]
    total = len(codes)
    log.info(
        "키움 대량 수집: 기간 %s~%s, 간격 %s, 종목 %d개 (완료 %d / 남음 %d), "
        "max_pages=%d, throttle 페이지 %.2fs·종목 %.2fs, 배치 %d",
        start_ymd, end_ymd, interval, total, len(done), len(pending),
        max_pages, page_throttle, symbol_throttle, batch,
    )
    if not pending:
        log.info("모든 종목 수집 완료(체크포인트). 다시 받으려면 --reset.")
        return

    by_date: dict[str, list[dict]] = defaultdict(list)
    batch_syms: list[str] = []
    t0 = time.monotonic()
    processed = 0
    base_done = len(done)   # 진행률 기준선(flush 가 done 을 갱신해도 고정)

    def flush() -> None:
        nonlocal by_date, batch_syms
        if not batch_syms:
            return
        merged_dates = 0
        for d in sorted(by_date):
            if store.merge_day(d, by_date[d]):
                merged_dates += 1
        done.update(batch_syms)
        prog["done"] = sorted(done)
        _save_progress(store, prog)
        log.info(
            "  💾 배치 저장: 종목 %d개 → 날짜 %d개 병합 (누적 완료 %d/%d). 보유 날짜 %d개",
            len(batch_syms), merged_dates, len(done), total,
            len(store.available_dates()),
        )
        by_date = defaultdict(list)
        batch_syms = []

    async with KiwoomClient(app_key, app_secret, base_url=base_url) as kc:
        try:
            for (code, name) in pending:
                try:
                    rows = await kc.fetch(
                        code, interval=interval, max_pages=max_pages,
                        throttle=page_throttle, stop_date=start_ymd,
                    )
                except Exception as e:  # noqa: BLE001 — 종목 단위 격리(전체 중단 금지)
                    log.warning("종목 %s(%s) 수집 실패 — 스킵(다음 실행 시 재시도): %s",
                                code, name, e)
                    continue
                got = 0
                span_lo = span_hi = ""
                for r in rows:
                    if start_ymd <= r.date <= end_ymd:
                        by_date[r.date].append(asdict(r))
                        got += 1
                        if not span_lo or r.date < span_lo:
                            span_lo = r.date
                        if r.date > span_hi:
                            span_hi = r.date
                batch_syms.append(code)
                processed += 1

                pct = (base_done + processed) / total * 100
                elapsed = time.monotonic() - t0
                rate = processed / elapsed if elapsed > 0 else 0
                remaining = len(pending) - processed
                eta = remaining / rate if rate > 0 else 0
                log.info(
                    "[%5.1f%%] %s(%s): %d행 %s~%s | 남음 %d개 · ETA %s",
                    pct, code, name, got, span_lo or "-", span_hi or "-",
                    remaining, _fmt_eta(eta),
                )

                if len(batch_syms) >= batch:
                    flush()
                if symbol_throttle:
                    await asyncio.sleep(symbol_throttle)
        finally:
            # Ctrl+C/예외에도 받은 만큼은 저장하고 체크포인트를 남긴다(재개 보장).
            flush()

    log.info(
        "키움 대량 수집 종료: 완료 %d/%d 종목, 보유 날짜 %d개 (%s~%s)",
        len(done), total, len(store.available_dates()),
        (store.available_dates() or ["-"])[0], (store.available_dates() or ["-"])[-1],
    )


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="키움 REST API 과거 분봉 대량 수집기 (2023+)")
    ap.add_argument("--start", default=os.getenv("KIWOOM_START") or "2023-01-01",
                    help="수집 시작일 YYYY-MM-DD (기본 2023-01-01; 2022 이전 미사용)")
    ap.add_argument("--end", default=os.getenv("KIWOOM_END") or date.today().strftime("%Y-%m-%d"),
                    help="수집 종료일 YYYY-MM-DD (기본 오늘)")
    ap.add_argument("--interval", default="1m",
                    help="분봉 간격 1m/3m/5m/10m/15m/30m/60m (기본 1m)")
    ap.add_argument("--max-pages", type=int, default=int(os.getenv("KIWOOM_MAX_PAGES") or 1000),
                    help="종목당 과거 페이지 상한(시작일 도달 시 조기 종료, 기본 1000)")
    ap.add_argument("--page-throttle", type=float, default=0.18,
                    help="페이지 간 대기(초) — 레이트리밋 회피 (기본 0.18)")
    ap.add_argument("--symbol-throttle", type=float, default=0.3,
                    help="종목 간 대기(초) (기본 0.3)")
    ap.add_argument("--batch", type=int, default=15,
                    help="이 종목 수마다 날짜 파일로 flush+체크포인트 (기본 15)")
    ap.add_argument("--reset", action="store_true",
                    help="체크포인트 초기화(처음부터 다시 수집)")
    args = ap.parse_args()

    start = _parse_date(args.start)
    floor = date(2023, 1, 1)
    if start < floor:
        log.warning("시작일 %s < 2023-01-01 — 2023-01-01 로 고정(2022 이전 미사용)", start)
        start = floor
    end = _parse_date(args.end)

    root = Path(__file__).parents[1]
    try:
        asyncio.run(collect(
            start=start, end=end, interval=args.interval, max_pages=args.max_pages,
            page_throttle=args.page_throttle, symbol_throttle=args.symbol_throttle,
            batch=args.batch, root=root, reset=args.reset,
        ))
    except KeyboardInterrupt:
        log.info("⏹ 사용자 중단(Ctrl+C) — 받은 데이터/체크포인트는 저장됨. 다시 실행하면 이어서 수집합니다.")
        return 0
    except KiwoomAuthError as e:
        log.error("키움 인증/설정 오류: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
