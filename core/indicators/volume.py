"""Volume spike detection."""
from __future__ import annotations

from collections.abc import Sequence


def volume_spike_ratio(volumes: Sequence[int], window: int = 20) -> float | None:
    """현재 거래량 / 직전 ``window`` 개의 평균.

    현재 캔들 자체는 평균에서 제외하여 현재 분의 폭발만 측정한다.
    데이터가 부족하거나 평균이 0이면 ``None``.
    """
    if window <= 0:
        raise ValueError("window must be > 0")
    n = len(volumes)
    if n < window + 1:
        return None
    recent = volumes[-(window + 1) : -1]
    avg = sum(recent) / window
    if avg <= 0:
        return None
    return volumes[-1] / avg
