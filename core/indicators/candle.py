"""Candle pattern detectors.

모든 함수는 ``CandleLike`` (``o/h/l/c`` 속성)만 요구한다.
"""
from __future__ import annotations

from typing import Protocol


class CandleLike(Protocol):
    o: float
    h: float
    l: float
    c: float


def _body(c: CandleLike) -> float:
    return abs(c.c - c.o)


def _range(c: CandleLike) -> float:
    return c.h - c.l


def _upper_shadow(c: CandleLike) -> float:
    return c.h - max(c.o, c.c)


def _lower_shadow(c: CandleLike) -> float:
    return min(c.o, c.c) - c.l


def is_hammer(c: CandleLike) -> bool:
    """body는 range의 ≤30%, 아래 그림자 ≥ body×2, 위 그림자는 짧다(≤range×10%)."""
    r = _range(c)
    if r <= 0:
        return False
    body = _body(c)
    if body / r > 0.30:
        return False
    return _lower_shadow(c) >= 2 * body and _upper_shadow(c) <= r * 0.10


def is_shooting_star(c: CandleLike) -> bool:
    """망치형 대칭. 위 그림자가 body의 2배 이상."""
    r = _range(c)
    if r <= 0:
        return False
    body = _body(c)
    if body / r > 0.30:
        return False
    return _upper_shadow(c) >= 2 * body and _lower_shadow(c) <= r * 0.10


def is_long_bullish(c: CandleLike) -> bool:
    """양봉이며 body가 range의 70% 이상."""
    r = _range(c)
    if r <= 0:
        return False
    return c.c > c.o and _body(c) / r >= 0.70


def is_long_bearish(c: CandleLike) -> bool:
    r = _range(c)
    if r <= 0:
        return False
    return c.c < c.o and _body(c) / r >= 0.70


def is_bullish_engulfing(prev: CandleLike, cur: CandleLike) -> bool:
    """prev 음봉 + cur 양봉, cur가 prev body를 완전히 감쌈."""
    if not (prev.c < prev.o and cur.c > cur.o):
        return False
    return cur.o <= prev.c and cur.c >= prev.o


def is_bearish_engulfing(prev: CandleLike, cur: CandleLike) -> bool:
    if not (prev.c > prev.o and cur.c < cur.o):
        return False
    return cur.o >= prev.c and cur.c <= prev.o
