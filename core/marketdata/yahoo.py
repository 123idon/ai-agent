"""Yahoo Finance 분봉 수집 클라이언트 (CLAUDE.md §18).

배치 ETL 전용 — 라이브 매매 경로(§15 traidair)와 무관한 과거 데이터 적재용이다.
Yahoo chart API(v8)는 키가 필요 없으며, 1분봉은 **최근 ~30일**만, 요청당 최대 7일
구간만 제공한다(그 이상은 빈 응답). 따라서 60일 백필 시 더 과거 구간은 비어 있을 수
있고, 그 날짜는 자연히 스킵된다.

타임스탬프는 UTC epoch이며 ``datetime.fromtimestamp(ts, KST)``로 KST 벽시계로 변환한다.
정규장(09:00~15:30 KST) 분봉만 보관한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import httpx

from core.time_utils import KST, MARKET_CLOSE, MARKET_OPEN

log = logging.getLogger(__name__)

_BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


@dataclass(frozen=True)
class CandleRow:
    symbol: str       # 저장 키 (종목 6자리 코드 또는 지수 심볼)
    date: str         # YYYYMMDD (KST)
    t: str            # HH:MM (KST)
    o: int
    h: int
    l: int
    c: int
    v: int


class YahooError(RuntimeError):
    pass


class YahooClient:
    def __init__(
        self, *, http_client: httpx.AsyncClient | None = None, timeout: float = 20.0,
    ) -> None:
        self._owns = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": _UA},
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._owns:
            await self._client.aclose()

    async def __aenter__(self) -> "YahooClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def fetch(
        self,
        yahoo_symbol: str,
        *,
        store_symbol: str | None = None,
        interval: str = "1m",
        period1: int | None = None,
        period2: int | None = None,
        range_: str | None = None,
        session_only: bool = True,
    ) -> list[CandleRow]:
        """단일 심볼 분봉 조회. period1/period2(epoch초) 또는 range_ 중 하나 사용."""
        params: dict[str, str] = {"interval": interval, "includePrePost": "false"}
        if period1 is not None and period2 is not None:
            params["period1"] = str(int(period1))
            params["period2"] = str(int(period2))
        else:
            params["range"] = range_ or "7d"

        url = _BASE + quote(yahoo_symbol)
        try:
            resp = await self._client.get(url, params=params)
        except httpx.HTTPError as e:
            raise YahooError(f"{yahoo_symbol}: {e}") from e
        if resp.status_code != 200:
            raise YahooError(f"{yahoo_symbol}: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as e:
            raise YahooError(f"{yahoo_symbol}: non-JSON") from e

        chart = (data or {}).get("chart") or {}
        if chart.get("error"):
            raise YahooError(f"{yahoo_symbol}: {chart['error']}")
        results = chart.get("result") or []
        if not results:
            return []
        res = results[0]
        ts = res.get("timestamp") or []
        quotes = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        opens = quotes.get("open") or []
        highs = quotes.get("high") or []
        lows = quotes.get("low") or []
        closes = quotes.get("close") or []
        vols = quotes.get("volume") or []

        key = store_symbol or yahoo_symbol
        out: list[CandleRow] = []
        for i, epoch in enumerate(ts):
            c = closes[i] if i < len(closes) else None
            if c is None:
                continue
            dt = datetime.fromtimestamp(int(epoch), KST)
            if session_only and not (MARKET_OPEN <= dt.time() <= MARKET_CLOSE):
                continue
            o = opens[i] if i < len(opens) and opens[i] is not None else c
            h = highs[i] if i < len(highs) and highs[i] is not None else c
            lo = lows[i] if i < len(lows) and lows[i] is not None else c
            v = vols[i] if i < len(vols) and vols[i] is not None else 0
            out.append(CandleRow(
                symbol=key, date=dt.strftime("%Y%m%d"), t=dt.strftime("%H:%M"),
                o=int(o), h=int(h), l=int(lo), c=int(c), v=int(v),
            ))
        return out
