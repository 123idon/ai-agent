"""볼린저 밴드 (Bollinger Bands).

CLAUDE.md §5.2 매수 타점 — '볼린저밴드 하단 반등 / 중단 돌파' 조건에 사용한다.
중심선 = SMA(period), 상·하단 = 중심선 ± num_std × 표준편차(모표준편차, ddof=0).

반환 길이는 항상 입력 길이와 같다. 워밍업 구간은 ``None``으로 채워 캔들 인덱스와
동기화한다(다른 지표와 동일 규약).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class BollingerBands:
    upper: list[float | None]
    middle: list[float | None]
    lower: list[float | None]


def bollinger_bands(
    values: Sequence[float], *, period: int = 20, num_std: float = 2.0,
) -> BollingerBands:
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(values)
    upper: list[float | None] = [None] * n
    middle: list[float | None] = [None] * n
    lower: list[float | None] = [None] * n
    if n < period:
        return BollingerBands(upper, middle, lower)
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        sd = var ** 0.5
        middle[i] = mean
        upper[i] = mean + num_std * sd
        lower[i] = mean - num_std * sd
    return BollingerBands(upper, middle, lower)
