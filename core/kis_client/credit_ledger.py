"""Track loan dates for credit (margin) positions.

KIS 신용 매도 시 LOAN_DT(매수 체결일, YYYYMMDD)가 필수다.
실행부는 신용 매수 체결 직후 record_buy(code)를 호출해 매핑을 저장하고,
매도 시 loan_dt(code)로 조회한다. CLAUDE.md §15.5 참조.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

_KST = timezone(timedelta(hours=9))


class CreditLedger:
    """File-backed (code → loan_dt 'YYYYMMDD') registry, KST 기준."""

    def __init__(self, state_path: Path) -> None:
        self._path = state_path
        self._lock = Lock()
        self._cache: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._cache = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._cache = {}
        else:
            self._cache = {}

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def record_buy(self, code: str, *, when: datetime | None = None) -> str:
        loan_dt = (when or datetime.now(_KST)).strftime("%Y%m%d")
        with self._lock:
            self._cache[code] = loan_dt
            self._flush()
        return loan_dt

    def loan_dt(self, code: str) -> str | None:
        with self._lock:
            return self._cache.get(code)

    def clear(self, code: str) -> None:
        with self._lock:
            self._cache.pop(code, None)
            self._flush()

    def all(self) -> dict[str, str]:
        with self._lock:
            return dict(self._cache)
