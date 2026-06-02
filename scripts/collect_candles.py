"""Yahoo Finance 분봉 수집기 (CLAUDE.md §18).

- 최초: 오늘 기준 과거 N일(기본 60) 분봉을 한 번에 수집 → data/candles/{date}.parquet.
- 증분(--incremental): 최근 2일만 받아 누락 날짜(주로 당일)를 추가. 매일 15:40 스케줄.
- **이미 있는 날짜는 스킵**(중복 수집 없음), **기존 파일은 삭제하지 않음**.
- 대상: config/universe.json (KOSPI200/KOSDAQ150 종목 + 코스피/코스닥/나스닥 지수).

Yahoo 1분봉 제약: 최근 ~30일만, 요청당 ≤7일. 더 과거 구간은 빈 응답 → 자연 스킵.

사용:
  python scripts/collect_candles.py                # 최초 60일 백필
  python scripts/collect_candles.py --days 60
  python scripts/collect_candles.py --incremental  # 당일 누적 (스케줄러용)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from core.marketdata import CandleStore, YahooClient, YahooError, all_targets, load_universe

log = logging.getLogger("collect_candles")

_WINDOW_DAYS = 7   # Yahoo 1m 요청당 최대 구간


def _windows(days: int) -> list[tuple[int, int]]:
    """now 기준 과거 days일을 7일 구간으로 분할한 (period1, period2) epoch 리스트."""
    now = int(datetime.now(timezone.utc).timestamp())
    start = now - days * 86400
    out: list[tuple[int, int]] = []
    cur = start
    while cur < now:
        nxt = min(cur + _WINDOW_DAYS * 86400, now)
        out.append((cur, nxt))
        cur = nxt
    return out


async def collect(
    *, days: int, interval: str, throttle: float, root: Path, incremental: bool,
) -> None:
    univ = load_universe(root / "config" / "universe.json")
    targets = all_targets(univ)
    store = CandleStore(root)
    existing = set(store.available_dates())

    windows = [(None, None)] if incremental else _windows(days)
    rng = "2d" if incremental else None

    log.info(
        "수집 시작: 종목/지수 %d개, %s, 기존날짜 %d개 (스킵)",
        len(targets), "증분(2d)" if incremental else f"백필 {days}일", len(existing),
    )

    by_date: dict[str, list[dict]] = defaultdict(list)
    seen_dates: set[str] = set()
    ok_syms = 0

    async with YahooClient() as yc:
        for n, tgt in enumerate(targets, 1):
            got = 0
            for (p1, p2) in windows:
                try:
                    rows = await yc.fetch(
                        tgt.yahoo, store_symbol=tgt.code, interval=interval,
                        period1=p1, period2=p2, range_=rng,
                    )
                except YahooError as e:
                    log.debug("fetch 실패 %s: %s", tgt.yahoo, e)
                    rows = []
                for r in rows:
                    if r.date in existing:      # 이미 저장된 날짜는 버림(스킵)
                        continue
                    by_date[r.date].append(asdict(r))
                    seen_dates.add(r.date)
                    got += 1
                if throttle:
                    await asyncio.sleep(throttle)
            if got:
                ok_syms += 1
            if n % 25 == 0:
                log.info("  진행 %d/%d 종목, 수집 날짜 %d개", n, len(targets), len(seen_dates))

    written = 0
    for date in sorted(seen_dates):
        if store.write_day(date, by_date[date]):
            written += 1
    log.info(
        "수집 완료: 신규 날짜 %d개 저장, 데이터 있는 종목 %d개. 총 보유 날짜 %d개",
        written, ok_syms, len(store.available_dates()),
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--throttle", type=float, default=0.25)
    ap.add_argument("--incremental", action="store_true")
    args = ap.parse_args()
    root = Path(__file__).parents[1]
    asyncio.run(collect(
        days=args.days, interval=args.interval, throttle=args.throttle,
        root=root, incremental=args.incremental,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
