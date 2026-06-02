"""AgentApiClient — ai-agent ↔ traidair 통합 API 클라이언트 (CLAUDE.md §22).

통합 원칙:
- **KIS API는 traidair만 호출**한다. ai-agent는 KIS 키를 모르며, 이 클라이언트로
  traidair의 ``/api/agent/*`` HTTP 엔드포인트만 호출한다.
- 인증은 **X-Agent-Key 헤더**로 한다(키는 ``KisClientConfig.agent_key``).
- 모든 응답은 HTTP 200 + ``ok`` 필드로 판정(§15.2 규칙 계승). ``ok:false``는
  ``KisBusinessError``, 네트워크/타임아웃은 ``KisTransportError``, 401은 인증 오류.

에이전트별 매핑:
- 스크리닝   → ``screen_candidates()``       GET  /api/agent/screen/candidates
- 시장상황   → ``market_snapshot()``         GET  /api/agent/market/snapshot
- 신호분석   → ``quote_indicators(code)``    GET  /api/agent/quote/:code/indicators
- 리스크     → ``risk_check()`` / ``positions()``  GET /api/agent/risk/check · /positions
- 주문실행   → ``order(...)``                POST /api/agent/order
- 학습부     → ``journal_append()`` / ``journal_today()``  POST·GET /api/agent/journal
- 백테스트   → ``backtest_run()``            POST /api/agent/backtest/run
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .config import KisClientConfig
from .exceptions import KisAuthError, KisBusinessError, KisTransportError

log = logging.getLogger(__name__)


class AgentApiClient:
    def __init__(
        self,
        config: KisClientConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._cfg = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(config.timeout_seconds),
            headers={"X-Agent-Key": config.agent_key},
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "AgentApiClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # ─────────────────────────── 내부 ───────────────────────────

    async def _with_retry(
        self, fn: Callable[[], Awaitable[httpx.Response]]
    ) -> httpx.Response:
        """6초 데드라인 + 지수 백오프 1회 재시도(§15.2)."""
        backoff = self._cfg.retry_initial_backoff_ms / 1000.0
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                return await fn()
            except httpx.HTTPError as e:  # noqa: PERF203
                last_exc = e
                if attempt == 0:
                    await asyncio.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    async def _request(
        self,
        method: str,
        route: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            resp = await self._with_retry(
                lambda: self._client.request(method, route, params=params, json=json)
            )
        except httpx.HTTPError as e:
            raise KisTransportError(f"{route}: {e}") from e

        if resp.status_code == 401:
            raise KisAuthError(f"{route}: unauthorized — X-Agent-Key 불일치")
        try:
            data = resp.json()
        except ValueError as e:
            raise KisTransportError(
                f"{route}: non-JSON response (status={resp.status_code})"
            ) from e
        if data.get("ok") is False:
            raise KisBusinessError(
                f"{route}: {data.get('error') or 'unknown'}", route=route, payload=data
            )
        return data

    # ─────────────────────────── 7개 엔드포인트 ───────────────────────────

    async def screen_candidates(
        self, *, market: str = "kospi", limit: int = 30
    ) -> dict[str, Any]:
        """[스크리닝] 거래대금 상위 후보 목록."""
        return await self._request(
            "GET", "/api/agent/screen/candidates",
            params={"market": market, "limit": limit},
        )

    async def market_snapshot(self) -> dict[str, Any]:
        """[시장상황] 매크로 지수 스냅샷."""
        return await self._request("GET", "/api/agent/market/snapshot")

    async def quote_indicators(
        self, code: str, *, tf: str = "1", date: str | None = None
    ) -> dict[str, Any]:
        """[신호분석] 종목 보조지표(RSI/MACD/MA/거래량비)."""
        params: dict[str, Any] = {"tf": tf}
        if date:
            params["date"] = date
        return await self._request(
            "GET", f"/api/agent/quote/{code}/indicators", params=params
        )

    async def risk_check(
        self,
        *,
        code: str = "",
        price: float | int = 0,
        qty: int = 0,
        order_type: str = "limit",
        account: str | None = None,
    ) -> dict[str, Any]:
        """[리스크] 주문 전 게이트 데이터(매수가능액·호가·동시보유)."""
        params: dict[str, Any] = {
            "code": code, "price": price, "qty": qty, "orderType": order_type,
        }
        if account:
            params["account"] = account
        return await self._request("GET", "/api/agent/risk/check", params=params)

    async def positions(self, *, account: str | None = None) -> dict[str, Any]:
        """[리스크] 보유/잔고."""
        params: dict[str, Any] = {}
        if account:
            params["account"] = account
        return await self._request("GET", "/api/agent/positions", params=params)

    async def order(
        self,
        *,
        side: str,
        code: str,
        qty: int,
        price: float | int,
        order_type: str = "limit",
        credit: bool = False,
        crdt_type: str | None = None,
        loan_date: str | None = None,
        account: str | None = None,
    ) -> dict[str, Any]:
        """[주문실행] 현금/신용 주문."""
        body: dict[str, Any] = {
            "side": side, "code": code, "qty": qty, "price": price,
            "orderType": order_type, "credit": credit,
        }
        if crdt_type:
            body["crdtType"] = crdt_type
        if loan_date:
            body["loanDate"] = loan_date
        if account:
            body["account"] = account
        return await self._request("POST", "/api/agent/order", json=body)

    async def journal_append(self, entry: dict[str, Any]) -> dict[str, Any]:
        """[학습부] 저널 기록(서버가 ts 스탬프 후 append-only JSONL)."""
        return await self._request("POST", "/api/agent/journal", json=entry)

    async def journal_today(self, *, limit: int = 200) -> dict[str, Any]:
        """[학습부] 오늘 저널 조회."""
        return await self._request(
            "GET", "/api/agent/journal/today", params={"limit": limit}
        )

    async def backtest_run(
        self,
        *,
        days: int | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """[백테스트] 실행 트리거(HTS 백테스트 버튼 연동)."""
        body: dict[str, Any] = {}
        if days:
            body["days"] = days
        if start:
            body["start"] = start
        if end:
            body["end"] = end
        return await self._request("POST", "/api/agent/backtest/run", json=body)
