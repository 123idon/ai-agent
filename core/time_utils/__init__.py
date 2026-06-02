"""KRX business-day calendar + KST datetime helpers. All timestamps must be tz-aware.

본 모듈은 두 가지를 제공한다(CLAUDE.md §13.8 — 모든 시각은 KST + 영업일 캘린더):

1. **KST 헬퍼**: ``KST`` tzinfo, ``kst_now()``, ``to_kst()``, ``ymd()``.
2. **백테스트 가상 시계(`SimClock`)**: 랜덤 과거 날짜 리플레이(§17)에서 전 에이전트가
   참조하는 단일 시간 소스. 에이전트는 모두 ``clock: Callable[[], datetime]``를 주입받으므로
   ``SimClock.now``를 넘기면 실제 ``datetime.now`` 대신 가상 시각으로 동작한다.
3. **영업일 유틸**: 주말 제외 + (옵션) 휴장일 집합. 랜덤 영업일 선택은
   ``random_business_day()``. 실제 거래일 여부(휴장/데이터 유무)는 데이터 소스로
   재확인하는 것이 권위적이므로, 휴장일 하드코딩에 의존하지 않는 설계다.
"""
from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone

KST = timezone(timedelta(hours=9))

# 정규장 운영 시간 (KRX). 동시호가/시간외는 본 시스템 범위 밖(§5.6).
MARKET_OPEN = time(9, 0, 0)
MARKET_CLOSE = time(15, 30, 0)

# 선택적 휴장일 집합. 비어 있어도 동작하며, 런타임에 실데이터 유무로 거래일을
# 재확인하므로 권위 소스가 아니다. 필요 시 확장한다(예: 임시 휴장).
HOLIDAYS: set[date] = set()


# ─────────────────────────── KST 헬퍼 ───────────────────────────


def kst_now() -> datetime:
    """현재 KST 시각 (tz-aware)."""
    return datetime.now(KST)


def to_kst(dt: datetime) -> datetime:
    """tz-aware datetime을 KST로 변환. naive는 거부(§13.8)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime is not allowed (CLAUDE.md §13.8)")
    return dt.astimezone(KST)


def ymd(d: date | datetime) -> str:
    """YYYYMMDD 문자열 (KIS/KRX basDd 형식)."""
    if isinstance(d, datetime):
        d = to_kst(d).date()
    return d.strftime("%Y%m%d")


def from_ymd(s: str) -> date:
    """YYYYMMDD → date."""
    return datetime.strptime(s, "%Y%m%d").date()


def at_kst(d: date, t: time) -> datetime:
    """날짜 + 시각 → KST tz-aware datetime."""
    return datetime(d.year, d.month, d.day, t.hour, t.minute, t.second, tzinfo=KST)


# ─────────────────────────── 영업일 ───────────────────────────


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5   # 5=토, 6=일


def is_business_day(d: date) -> bool:
    """주말/휴장일이 아니면 True (휴장일 집합은 보조 — 권위는 실데이터)."""
    return not is_weekend(d) and d not in HOLIDAYS


def prev_business_day(d: date) -> date:
    """직전 영업일 (전일 거래대금 기반 스크리닝용, §2.2.1)."""
    cur = d - timedelta(days=1)
    while not is_business_day(cur):
        cur -= timedelta(days=1)
    return cur


def next_business_day(d: date) -> date:
    cur = d + timedelta(days=1)
    while not is_business_day(cur):
        cur += timedelta(days=1)
    return cur


def business_days(start: date, end: date) -> list[date]:
    """[start, end] 사이의 모든 영업일 (포함)."""
    out: list[date] = []
    cur = start
    while cur <= end:
        if is_business_day(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def random_business_day(
    start: date,
    end: date,
    *,
    rng: random.Random | None = None,
) -> date:
    """[start, end] 사이에서 균등 랜덤 영업일 1개 선택 (§17 랜덤 백테스트).

    실제 거래일(휴장/데이터 유무)은 데이터 소스로 재확인하는 것이 권위적이므로,
    여기서는 주말/휴장집합만 거른 뒤 선택한다.
    """
    days = business_days(start, end)
    if not days:
        raise ValueError(f"no business days in [{start}, {end}]")
    r = rng or random.Random()
    return r.choice(days)


def session_minutes(
    d: date,
    *,
    open_time: time = MARKET_OPEN,
    close_time: time = MARKET_CLOSE,
    step_minutes: int = 1,
) -> Iterator[datetime]:
    """정규장 09:00→15:30을 step_minutes 간격의 KST datetime으로 순회(§17 리플레이)."""
    cur = at_kst(d, open_time)
    end = at_kst(d, close_time)
    delta = timedelta(minutes=step_minutes)
    while cur <= end:
        yield cur
        cur += delta


# ─────────────────────────── 가상 시계 ───────────────────────────


class SimClock:
    """백테스트 리플레이용 가변 가상 시계 (§17).

    전 에이전트가 ``clock=sim_clock.now``로 주입받아 동일한 가상 시각을 본다.
    ``set()``/``advance()``로 운영자(러너)가 시간을 전진시킨다. 항상 KST tz-aware.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = to_kst(start) if start is not None else kst_now()

    def now(self) -> datetime:
        return self._now

    def set(self, dt: datetime) -> None:
        self._now = to_kst(dt)

    def advance(self, *, seconds: float = 0, minutes: float = 0) -> datetime:
        self._now = self._now + timedelta(seconds=seconds, minutes=minutes)
        return self._now

    @property
    def date_str(self) -> str:
        """현재 가상 시각의 YYYYMMDD."""
        return ymd(self._now)

    @property
    def hhmm(self) -> str:
        """현재 가상 시각의 'HHMM' (캔들 컷오프 비교용)."""
        return self._now.strftime("%H%M")

    def __repr__(self) -> str:  # pragma: no cover - 디버그 편의
        return f"SimClock({self._now.isoformat()})"


__all__ = [
    "KST",
    "MARKET_OPEN",
    "MARKET_CLOSE",
    "HOLIDAYS",
    "kst_now",
    "to_kst",
    "ymd",
    "from_ymd",
    "at_kst",
    "is_weekend",
    "is_business_day",
    "prev_business_day",
    "next_business_day",
    "business_days",
    "random_business_day",
    "session_minutes",
    "SimClock",
]
