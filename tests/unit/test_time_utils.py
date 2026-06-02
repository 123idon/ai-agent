"""core.time_utils — KST 헬퍼 / 영업일 / SimClock (CLAUDE.md §13.8, §17)."""
from __future__ import annotations

import random
from datetime import date, datetime, time

import pytest

from core.time_utils import (
    KST,
    SimClock,
    at_kst,
    business_days,
    from_ymd,
    is_business_day,
    prev_business_day,
    random_business_day,
    session_minutes,
    to_kst,
    ymd,
)


def test_ymd_and_from_ymd_roundtrip() -> None:
    d = date(2024, 3, 15)
    assert ymd(d) == "20240315"
    assert from_ymd("20240315") == d


def test_to_kst_rejects_naive() -> None:
    with pytest.raises(ValueError):
        to_kst(datetime(2024, 1, 1, 9, 0))


def test_is_business_day_weekend() -> None:
    assert is_business_day(date(2024, 1, 5))      # 금
    assert not is_business_day(date(2024, 1, 6))  # 토
    assert not is_business_day(date(2024, 1, 7))  # 일


def test_prev_business_day_skips_weekend() -> None:
    # 2024-01-08(월)의 직전 영업일은 2024-01-05(금)
    assert prev_business_day(date(2024, 1, 8)) == date(2024, 1, 5)


def test_business_days_excludes_weekend() -> None:
    days = business_days(date(2024, 1, 1), date(2024, 1, 7))
    assert date(2024, 1, 6) not in days and date(2024, 1, 7) not in days
    assert all(d.weekday() < 5 for d in days)


def test_random_business_day_deterministic_with_seed() -> None:
    a = random_business_day(date(2023, 1, 1), date(2023, 12, 31), rng=random.Random(42))
    b = random_business_day(date(2023, 1, 1), date(2023, 12, 31), rng=random.Random(42))
    assert a == b
    assert a.weekday() < 5


def test_session_minutes_bounds() -> None:
    mins = list(session_minutes(date(2024, 1, 2), step_minutes=1))
    assert mins[0] == at_kst(date(2024, 1, 2), time(9, 0))
    assert mins[-1] == at_kst(date(2024, 1, 2), time(15, 30))
    assert len(mins) == (15 * 60 + 30) - (9 * 60) + 1   # 391분 (09:00~15:30 포함)


def test_simclock_set_advance() -> None:
    clk = SimClock(at_kst(date(2024, 1, 2), time(9, 0)))
    assert clk.date_str == "20240102"
    assert clk.hhmm == "0900"
    clk.advance(minutes=31)
    assert clk.hhmm == "0931"
    assert clk.now().tzinfo == KST
    clk.set(at_kst(date(2024, 1, 2), time(15, 25)))
    assert clk.hhmm == "1525"
