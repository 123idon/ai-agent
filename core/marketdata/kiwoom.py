"""키움증권 REST API 분봉 수집 클라이언트 (CLAUDE.md §18 보강).

**데이터 수집 전용** — 라이브 매매 경로(§15 traidair/KIS)와 완전히 분리된 배치 ETL이다.
기존 KIS API는 그대로 유지되며, 본 클라이언트는 ``data/candles/{date}.parquet`` 백필
용도로만 쓰인다.

키움 REST API 개요 (apiportal / api.kiwoom.com):
- 토큰 발급: ``POST /oauth2/token``  body ``{grant_type, appkey, secretkey}``
  → ``{token, token_type, expires_dt, return_code}`` (return_code 0 = 정상).
- 주식분봉차트: ``POST /api/dostk/chart``  헤더 ``api-id: ka10080``
  body ``{stk_cd, tic_scope, upd_stkpc_tp}`` → ``{stk_min_pole_chart_qry:[...]}``.
  응답 헤더 ``cont-yn``/``next-key`` 로 과거 방향 페이지네이션.

분봉 item 필드: ``cntr_tm``(체결시간 YYYYMMDDHHMMSS), ``cur_prc``(종가), ``open_pric``,
``high_pric``, ``low_pric``, ``trde_qty``(거래량). 가격은 부호(+/-) 접두가 붙어 오므로
절댓값 정수로 정규화한다. 정규장(09:00~15:30 KST) 분봉만 보관한다.

CandleRow 스키마는 YahooClient 와 동일하므로 ``CandleStore`` 에 그대로 적재된다.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx

from core.time_utils import KST, MARKET_CLOSE, MARKET_OPEN

from .yahoo import CandleRow

log = logging.getLogger(__name__)

_DEFAULT_BASE = "https://api.kiwoom.com"

# tic_scope: 1/3/5/10/15/30/45/60 분
_INTERVAL_TO_TIC = {
    "1m": "1", "1": "1",
    "3m": "3", "3": "3",
    "5m": "5", "5": "5",
    "10m": "10", "10": "10",
    "15m": "15", "15": "15",
    "30m": "30", "30": "30",
    "60m": "60", "60": "60",
}


class KiwoomError(RuntimeError):
    pass


class KiwoomAuthError(KiwoomError):
    pass


def _signed_int(v: object) -> int:
    """'+74000' / '-73900' / '74000' / '' → 74000 (절댓값 정수)."""
    s = str(v).strip()
    if not s:
        return 0
    neg = s.startswith("-")
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return 0
    n = int(digits)
    return -n if neg else n


class KiwoomClient:
    """키움 REST API 분봉 클라이언트 (수집 전용)."""

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        *,
        base_url: str = _DEFAULT_BASE,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        if not app_key or not app_secret:
            raise KiwoomAuthError("키움 app_key/app_secret 가 필요합니다")
        self._app_key = app_key
        self._app_secret = app_secret
        self._base = base_url.rstrip("/")
        self._owns = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={"Content-Type": "application/json;charset=UTF-8"},
            follow_redirects=True,
        )
        self._token: str | None = None

    async def close(self) -> None:
        if self._owns:
            await self._client.aclose()

    async def __aenter__(self) -> "KiwoomClient":
        await self.ensure_token()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ─────────────────────────── 토큰 ───────────────────────────

    async def ensure_token(self) -> str:
        if self._token:
            return self._token
        url = f"{self._base}/oauth2/token"
        body = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "secretkey": self._app_secret,
        }
        try:
            resp = await self._client.post(url, json=body)
        except httpx.HTTPError as e:
            raise KiwoomError(f"token request failed: {e}") from e
        if resp.status_code != 200:
            raise KiwoomAuthError(f"token HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        # 키움은 비즈니스 실패도 200으로 줄 수 있다 → return_code 로 판정.
        if str(data.get("return_code", "0")) not in ("0", "0000"):
            raise KiwoomAuthError(
                f"token return_code={data.get('return_code')}: {data.get('return_msg')}"
            )
        tok = data.get("token") or data.get("access_token")
        if not tok:
            raise KiwoomAuthError(f"token missing in response: {str(data)[:200]}")
        self._token = str(tok)
        log.info("키움 토큰 발급 완료 (만료 %s)", data.get("expires_dt", "?"))
        return self._token

    # ─────────────────────────── 분봉 ───────────────────────────

    async def fetch(
        self,
        code: str,
        *,
        store_symbol: str | None = None,
        interval: str = "1m",
        max_pages: int = 20,
        adjust: bool = True,
        session_only: bool = True,
        throttle: float = 0.2,
        stop_date: str | None = None,
        max_retries: int = 4,
    ) -> list[CandleRow]:
        """종목 분봉을 과거 방향으로 페이지네이션하며 수집.

        ``max_pages`` 페이지(또는 ``cont-yn=N``)까지 모은다. 키움 ka10080 은 최신→과거
        순으로 반환하므로 페이지를 넘길수록 더 과거를 받는다. 반환은 시간 오름차순.

        - ``stop_date`` (YYYYMMDD): 그 날짜보다 더 과거 분봉이 보이면 그 페이지까지만 받고
          중단(대량 수집에서 2023-01-01 같은 시작 경계를 만나면 멈춤).
        - 레이트리밋(429)·일시 오류는 지수 백오프로 ``max_retries`` 회 재시도.
        """
        await self.ensure_token()
        tic = _INTERVAL_TO_TIC.get(interval, "1")
        key = store_symbol or code
        url = f"{self._base}/api/dostk/chart"
        body = {
            "stk_cd": code,
            "tic_scope": tic,
            "upd_stkpc_tp": "1" if adjust else "0",
        }
        rows: dict[tuple[str, str], CandleRow] = {}   # (date,t) 중복 제거
        cont_yn = "N"
        next_key = ""
        for _page in range(max_pages):
            headers = {
                "authorization": f"Bearer {self._token}",
                "api-id": "ka10080",
                "cont-yn": cont_yn,
                "next-key": next_key,
            }
            resp = await self._post_with_retry(url, body, headers, code, max_retries)
            try:
                data = resp.json()
            except ValueError as e:
                raise KiwoomError(f"{code}: non-JSON chart response") from e
            if str(data.get("return_code", "0")) not in ("0", "0000"):
                # 비즈니스 오류(예: 권한/종목 없음)는 비치명 — 지금까지 모은 것 반환.
                log.debug("키움 %s chart return_code=%s msg=%s",
                          code, data.get("return_code"), data.get("return_msg"))
                break

            items = (
                data.get("stk_min_pole_chart_qry")
                or data.get("stk_min_pole_chart")
                or []
            )
            page_oldest = "99999999"
            for it in items:
                row = self._parse_item(key, it, session_only=session_only)
                if row is not None:
                    rows[(row.date, row.t)] = row
                    if row.date < page_oldest:
                        page_oldest = row.date

            cont_yn = (resp.headers.get("cont-yn") or "N").strip()
            next_key = (resp.headers.get("next-key") or "").strip()
            # 시작 경계 도달 → 더 과거는 받지 않는다.
            if stop_date is not None and page_oldest <= stop_date:
                break
            if throttle:
                await asyncio.sleep(throttle)
            if cont_yn != "Y" or not next_key:
                break

        out = sorted(rows.values(), key=lambda r: (r.date, r.t))
        return out

    async def _post_with_retry(
        self, url: str, body: dict, headers: dict, code: str, max_retries: int,
    ) -> httpx.Response:
        """POST + 401 재발급 + 429/일시오류 지수 백오프 재시도."""
        backoff = 0.5
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = await self._client.post(url, json=body, headers=headers)
            except httpx.HTTPError as e:
                last_exc = KiwoomError(f"{code}: chart request failed: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            if resp.status_code == 401:
                # 토큰 만료 → 재발급 후 재시도.
                self._token = None
                await self.ensure_token()
                headers["authorization"] = f"Bearer {self._token}"
                last_exc = KiwoomAuthError(f"{code}: 401 (token refreshed)")
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                # 레이트리밋/서버 오류 → 백오프 후 재시도.
                last_exc = KiwoomError(f"{code}: HTTP {resp.status_code}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            if resp.status_code != 200:
                raise KiwoomError(f"{code}: chart HTTP {resp.status_code}: {resp.text[:200]}")
            return resp
        raise last_exc or KiwoomError(f"{code}: chart failed after {max_retries} retries")

    @staticmethod
    def _parse_item(
        key: str, it: dict, *, session_only: bool,
    ) -> CandleRow | None:
        cntr = str(it.get("cntr_tm") or it.get("dt") or "").strip()
        digits = "".join(ch for ch in cntr if ch.isdigit())
        if len(digits) < 12:
            return None
        try:
            dt = datetime.strptime(digits[:12], "%Y%m%d%H%M").replace(tzinfo=KST)
        except ValueError:
            return None
        if session_only and not (MARKET_OPEN <= dt.time() <= MARKET_CLOSE):
            return None
        c = abs(_signed_int(it.get("cur_prc")))
        if c <= 0:
            return None
        o = abs(_signed_int(it.get("open_pric"))) or c
        h = abs(_signed_int(it.get("high_pric"))) or c
        lo = abs(_signed_int(it.get("low_pric"))) or c
        v = abs(_signed_int(it.get("trde_qty")))
        return CandleRow(
            symbol=key,
            date=dt.strftime("%Y%m%d"),
            t=dt.strftime("%H:%M"),
            o=int(o), h=int(h), l=int(lo), c=int(c), v=int(v),
        )
