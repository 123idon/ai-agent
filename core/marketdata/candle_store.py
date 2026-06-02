"""로컬 분봉 캐시 저장소 (CLAUDE.md §18).

``data/candles/{YYYYMMDD}.parquet`` — 하루치 전 종목 분봉을 한 파일에 보관한다.
- **날짜별 저장**: 파일 1개 = 1거래일.
- **무삭제**: 기존 파일은 절대 덮어쓰지 않는다(``overwrite`` 명시 시에만).
- **스킵**: 이미 있는 날짜는 수집을 건너뛴다.
- 백테스트는 ``available_dates()``의 날짜만 사용한다(로컬 우선).

컬럼: symbol, date, t, o, h, l, c, v. 백테스트 중 같은 날짜를 반복 조회하므로
날짜→{code→rows} 메모리 캐시(최대 3일)로 parquet 재파싱을 피한다.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

_COLUMNS = ["symbol", "date", "t", "o", "h", "l", "c", "v"]


class CandleStore:
    def __init__(self, root: Path) -> None:
        self.dir = Path(root) / "data" / "candles"
        self._cache: dict[str, dict[str, list[dict]]] = {}
        self._cache_order: list[str] = []
        # 일봉 집계(§5.2 일봉 게이트) 영구 캐시: date → {code → 일봉 dict}.
        # 과거 날짜의 일봉은 불변이라 한 번 계산하면 모든 세션/분에서 재사용한다
        # (get_daily_chart 가 매 분·매 후보마다 parquet 17개를 다시 읽던 병목 제거).
        # 종목당 1캔들이라 가벼워 절대 evict 하지 않는다.
        self._daily_cache: dict[str, dict[str, dict]] = {}

    def path(self, date: str) -> Path:
        return self.dir / f"{date}.parquet"

    def has_date(self, date: str) -> bool:
        return self.path(date).exists()

    def available_dates(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(p.stem for p in self.dir.glob("*.parquet"))

    # ─────────────────────────── 쓰기 ───────────────────────────

    def write_day(self, date: str, rows: list[dict], *, overwrite: bool = False) -> bool:
        """하루치 분봉 기록. 이미 존재하면(overwrite=False) 스킵하고 False 반환."""
        p = self.path(date)
        if p.exists() and not overwrite:
            return False
        if not rows:
            return False
        self.dir.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(rows)
        for col in _COLUMNS:
            if col not in df.columns:
                df[col] = 0 if col not in ("symbol", "date", "t") else ""
        df = df[_COLUMNS].sort_values(["symbol", "t"]).reset_index(drop=True)
        tmp = p.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False)
        os.replace(tmp, p)
        self._cache.pop(date, None)
        log.info("CANDLES 저장 %s: %d행 / %d종목", date, len(df), df["symbol"].nunique())
        return True

    def merge_day(self, date: str, rows: list[dict]) -> int:
        """하루치 분봉을 기존 날짜 파일에 **병합**한다(없으면 새로 생성).

        대량 수집(키움)이 종목 단위로 진행돼 같은 날짜 파일에 종목을 점진적으로 추가할 때
        쓴다. ``(symbol, t)`` 중복은 **신규 행(키움 우선)** 을 남긴다. 반환은 병합 후 총 행 수.
        ``write_day`` 와 달리 기존 파일이 있어도 스킵하지 않고 합친다.
        """
        if not rows:
            return 0
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.path(date)
        frames = []
        if p.exists():
            try:
                frames.append(pd.read_parquet(p))
            except Exception:  # noqa: BLE001 — 손상 파일은 새로 쓴다
                log.warning("기존 %s 읽기 실패 — 새로 작성", p)
        new_df = pd.DataFrame(rows)
        frames.append(new_df)
        df = pd.concat(frames, ignore_index=True)
        for col in _COLUMNS:
            if col not in df.columns:
                df[col] = 0 if col not in ("symbol", "date", "t") else ""
        df = df[_COLUMNS]
        # (symbol, t) 중복 제거 — keep="last" 로 신규(키움) 행을 남긴다.
        df = df.drop_duplicates(subset=["symbol", "t"], keep="last")
        df = df.sort_values(["symbol", "t"]).reset_index(drop=True)
        tmp = p.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False)
        os.replace(tmp, p)
        # 캐시 무효화(이 날짜의 원본/일봉 집계 모두).
        self._cache.pop(date, None)
        self._daily_cache.pop(date, None)
        return len(df)

    # ─────────────────────────── 읽기 ───────────────────────────

    def _load(self, date: str) -> dict[str, list[dict]]:
        if date in self._cache:
            return self._cache[date]
        by_code: dict[str, list[dict]] = {}
        p = self.path(date)
        if p.exists():
            df = pd.read_parquet(p)
            for sym, g in df.sort_values("t").groupby("symbol"):
                by_code[str(sym)] = g[["t", "date", "o", "h", "l", "c", "v"]].to_dict("records")
        self._cache[date] = by_code
        self._cache_order.append(date)
        while len(self._cache_order) > 3:
            old = self._cache_order.pop(0)
            self._cache.pop(old, None)
        return by_code

    def read_symbol(self, date: str, code: str) -> list[dict]:
        """해당 날짜·종목의 분봉 목록(시간순). 없으면 빈 리스트."""
        return list(self._load(date).get(code, []))

    def symbols_on(self, date: str) -> list[str]:
        return list(self._load(date).keys())

    # ─────────────────────── 일봉 집계 (영구 캐시) ───────────────────────

    def _ensure_daily(self, date: str) -> dict[str, dict]:
        cached = self._daily_cache.get(date)
        if cached is not None:
            return cached
        agg: dict[str, dict] = {}
        for code, rows in self._load(date).items():
            if not rows:
                continue
            agg[code] = {
                "t": date, "date": date,
                "o": int(rows[0]["o"]),
                "h": max(int(r["h"]) for r in rows),
                "l": min(int(r["l"]) for r in rows),
                "c": int(rows[-1]["c"]),
                "v": sum(int(r["v"]) for r in rows),
            }
        self._daily_cache[date] = agg
        return agg

    def daily_aggregate(self, date: str, code: str) -> dict | None:
        """해당 날짜·종목의 일봉 집계(o/h/l/c/v) 1캔들. 없으면 None.

        과거 날짜는 불변이므로 영구 캐시한다 — get_daily_chart 의 parquet 재파싱 제거.
        """
        return self._ensure_daily(date).get(code)
