"""KisClient — traidair proxy를 통한 KIS Open API 비동기 클라이언트.

CLAUDE.md §15.2 호출 규칙:
- 모든 응답이 HTTP 200이므로 반드시 body의 ok 필드로 판정
- 6초 데드라인 + 지수 백오프 1회 재시도 (200ms → 800ms)
- 인증 류 오류 감지 시 /api/kis/token 재발급 트리거 후 원 요청 1회 재시도
- 신용 매수 체결 후 CreditLedger.record_buy(code) 자동 호출, 매도 시 LOAN_DT 자동 주입
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, Callable

import httpx

from .config import KisClientConfig
from .credit_ledger import CreditLedger
from .paper_broker import PaperBroker
from .exceptions import (
    KisAuthError,
    KisBusinessError,
    KisModeMismatchError,
    KisTransportError,
)
from .models import (
    BalanceSnapshot,
    CancelAction,
    CancelResult,
    ChartResponse,
    DartListResponse,
    InvestorSeries,
    MarketDataResponse,
    Mode,
    OrderableAmount,
    OrderbookSnapshot,
    OrderResult,
    OrderType,
    Position,
    PriceSnapshot,
    Side,
    TokenSlice,
    UnfilledOrder,
    VolumeRankResponse,
)

log = logging.getLogger(__name__)


# traidair가 전달하는 KIS 인증 실패 메시지 패턴
_AUTH_ERROR_PATTERNS = (
    "토큰 없음",
    "토큰 발급 실패",
    "EGW00121",  # KIS access_token 만료
    "EGW00122",
    "EGW00123",
)


def _is_auth_error(message: str | None) -> bool:
    if not message:
        return False
    return any(p in message for p in _AUTH_ERROR_PATTERNS)


class KisClient:
    def __init__(
        self,
        config: KisClientConfig,
        *,
        credit_ledger: CreditLedger | None = None,
        paper_broker: PaperBroker | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._cfg = config
        self._ledger = credit_ledger
        # paper(모의) 모드: 주문/잔고만 가상 처리. 미주입 시 in-memory 폴백.
        self._paper: PaperBroker | None = None
        if config.simulate_orders:
            self._paper = paper_broker or PaperBroker(
                persist_path=None, start_cash=config.paper_start_cash,
            )
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(config.timeout_seconds),
        )

    @property
    def mode(self) -> Mode:
        return self._cfg.mode

    @property
    def simulate_orders(self) -> bool:
        return self._paper is not None

    @property
    def account(self) -> str:
        return self._cfg.account

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "KisClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # ─────────────────────────── 내부 ───────────────────────────

    def _auth_body(self) -> dict[str, str]:
        return {
            "appKey": self._cfg.app_key,
            "appSecret": self._cfg.app_secret,
            "mode": self._cfg.traidair_mode,
        }

    async def _post(
        self,
        route: str,
        payload: dict[str, Any],
        *,
        _reissued: bool = False,
    ) -> dict[str, Any]:
        body = {**self._auth_body(), **payload}
        try:
            response = await self._with_retry(
                lambda: self._client.post(route, json=body)
            )
        except httpx.HTTPError as e:
            raise KisTransportError(f"{route}: {e}") from e

        try:
            data = response.json()
        except ValueError as e:
            raise KisTransportError(
                f"{route}: non-JSON response (status={response.status_code})"
            ) from e

        if not data.get("ok"):
            err = data.get("error") or "unknown"
            if _is_auth_error(err) and not _reissued and route != "/api/kis/token":
                log.warning("auth error on %s: %s — re-issuing token", route, err)
                await self.fetch_token()
                return await self._post(route, payload, _reissued=True)
            if _is_auth_error(err):
                raise KisAuthError(f"{route}: {err}")
            raise KisBusinessError(err, route=route, payload=data)

        return data

    async def _get(self, route: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._with_retry(
                lambda: self._client.get(route, params=params)
            )
        except httpx.HTTPError as e:
            raise KisTransportError(f"{route}: {e}") from e
        try:
            return response.json()
        except ValueError as e:
            raise KisTransportError(
                f"{route}: non-JSON response (status={response.status_code})"
            ) from e

    async def _with_retry(
        self, send: Callable[[], Awaitable[httpx.Response]]
    ) -> httpx.Response:
        delay_ms = self._cfg.retry_initial_backoff_ms
        last_exc: Exception | None = None
        for attempt in range(2):  # 첫 시도 + 1회 재시도
            try:
                return await send()
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                if attempt == 0:
                    jitter = random.uniform(0.8, 1.2)
                    await asyncio.sleep((delay_ms / 1000.0) * jitter)
                    delay_ms = min(delay_ms * 4, self._cfg.retry_max_backoff_ms)
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    def _require_mode(self, allowed: Mode) -> None:
        if self._cfg.mode != allowed:
            raise KisModeMismatchError(
                f"this call requires mode={allowed.value}, current={self._cfg.mode.value}"
            )

    # ─────────────────────────── 토큰 ───────────────────────────

    async def fetch_token(self) -> TokenSlice:
        data = await self._post("/api/kis/token", {})
        return TokenSlice(token=data["token"])

    # ─────────────────────────── 시세 ───────────────────────────

    async def get_chart(
        self,
        code: str,
        *,
        date: str | None = None,
        tf: str = "1",
    ) -> ChartResponse:
        data = await self._post(
            "/api/kis/chart",
            {"code": code, "date": date, "tf": tf},
        )
        return ChartResponse(
            code=data["code"],
            date=data["date"],
            prevDate=data.get("prevDate"),
            tf=data.get("tf", tf),
            candles=data.get("candles", []),
            prevCount=data.get("prevCount", 0),
            todayCount=data.get("todayCount", 0),
        )

    async def get_daily_chart(
        self,
        code: str,
        *,
        lookback: int = 40,
    ) -> ChartResponse:
        """일봉 게이트(§5.2)용 일봉 캔들. traidair chart 라우트에 ``tf="D"`` 로 요청한다.

        traidair가 일봉(``D``)을 지원해야 동작한다(미지원 시 ``ok:false`` →
        ``KisBusinessError``; 호출자 분석부가 best-effort로 None 처리해 분봉만으로 판정).
        """
        del lookback  # traidair가 제공 깊이를 결정
        return await self.get_chart(code, tf="D")

    async def get_orderbook(self, code: str) -> OrderbookSnapshot:
        data = await self._post("/api/kis/orderbook", {"code": code})
        return OrderbookSnapshot(
            asks=data["asks"],
            bids=data["bids"],
            totalAsk=data["totalAsk"],
            totalBid=data["totalBid"],
            strength=data["strength"],
        )

    async def get_price(self, code: str) -> PriceSnapshot:
        data = await self._post("/api/kis/price", {"code": code})
        return PriceSnapshot.model_validate(
            {k: v for k, v in data.items() if k != "ok"}
        )

    async def get_volume_rank(
        self,
        *,
        market: str = "0000",
        rank_by: int = 3,
        min_price: int | None = None,
        max_price: int | None = None,
        top_n: int = 30,
    ) -> VolumeRankResponse:
        payload: dict[str, Any] = {
            "market": market,
            "rankBy": rank_by,
            "topN": top_n,
        }
        if min_price is not None:
            payload["minPrice"] = min_price
        if max_price is not None:
            payload["maxPrice"] = max_price
        data = await self._post("/api/kis/volume-rank", payload)
        return VolumeRankResponse(
            market=data["market"],
            rankBy=str(data["rankBy"]),
            items=data["items"],
        )

    async def get_investor(self, code: str) -> InvestorSeries:
        data = await self._post("/api/kis/investor", {"code": code})
        return InvestorSeries(code=data["code"], series=data["series"])

    # ─────────────────────────── 주문 ───────────────────────────

    async def place_order(
        self,
        *,
        side: Side,
        code: str,
        qty: int,
        price: int = 0,
        order_type: OrderType = OrderType.LIMIT,
    ) -> OrderResult:
        if self._paper is not None:
            return await self._paper_fill(side, code, qty, price)
        data = await self._post(
            "/api/kis/order",
            {
                "account": self._cfg.account,
                "side": side.value,
                "code": code,
                "qty": qty,
                "price": price,
                "orderType": order_type.value,
            },
        )
        return OrderResult(ordNo=str(data.get("ordNo") or ""), msg=data.get("msg"))

    async def _paper_fill(
        self, side: Side, code: str, qty: int, price: int,
    ) -> OrderResult:
        """모의 가상 체결. 시장가(price<=0)는 실전 현재가로 체결가를 결정한다."""
        assert self._paper is not None
        fill_price = float(price)
        if fill_price <= 0:
            snap = await self.get_price(code)   # 실전 시세로 체결가 결정
            fill_price = float(snap.price)
        ord_no = self._paper.fill(side, code, qty, fill_price)
        return OrderResult(ordNo=ord_no, msg=f"PAPER SIMULATED @{int(fill_price)}")

    async def place_credit_order(
        self,
        *,
        side: Side,
        code: str,
        qty: int,
        price: int = 0,
        order_type: OrderType = OrderType.LIMIT,
        crdt_type: str | None = None,
        loan_date: str | None = None,
    ) -> OrderResult:
        """신용 매수/매도 — 실전 모드 전용.

        매도 시 loan_date 미지정이면 CreditLedger에서 자동 조회한다.
        매수 성공 시 CreditLedger.record_buy(code)로 매수 일자를 기록한다.
        """
        if self._paper is not None:
            # 모의 모드는 신용을 현금 가상 체결로 처리(실주문 없음)
            return await self._paper_fill(side, code, qty, price)
        self._require_mode(Mode.LIVE)
        if side == Side.SELL and not loan_date:
            if self._ledger is None:
                raise KisBusinessError(
                    "credit sell requires loan_date or CreditLedger",
                    route="/api/kis/order-credit",
                )
            loan_date = self._ledger.loan_dt(code)
            if not loan_date:
                raise KisBusinessError(
                    f"no loan_dt recorded for {code}",
                    route="/api/kis/order-credit",
                )

        payload: dict[str, Any] = {
            "account": self._cfg.account,
            "side": side.value,
            "code": code,
            "qty": qty,
            "price": price,
            "orderType": order_type.value,
        }
        if crdt_type:
            payload["crdtType"] = crdt_type
        if loan_date:
            payload["loanDate"] = loan_date

        data = await self._post("/api/kis/order-credit", payload)
        result = OrderResult(
            ordNo=str(data.get("ordNo") or ""),
            krxFwdgOrgno=data.get("krxFwdgOrgno"),
            ordTime=data.get("ordTime"),
            msg=data.get("msg"),
        )
        if side == Side.BUY and self._ledger is not None:
            self._ledger.record_buy(code)
        return result

    async def cancel_order(
        self,
        *,
        org_ord_no: str,
        krx_fwdg_orgno: str,
        action: CancelAction = CancelAction.CANCEL,
        qty: int = 0,
        price: int = 0,
        order_type: OrderType = OrderType.LIMIT,
        qty_all: bool = True,
    ) -> CancelResult:
        if self._paper is not None:
            # 모의 모드는 즉시 전량 체결이라 미체결/취소가 없다 — no-op 성공.
            return CancelResult(
                ordNo=org_ord_no, action=action, msg="PAPER no-op (instant fill)",
            )
        data = await self._post(
            "/api/kis/order-cancel",
            {
                "account": self._cfg.account,
                "orgOrdNo": org_ord_no,
                "krxFwdgOrgno": krx_fwdg_orgno,
                "action": action.value,
                "qty": qty,
                "price": price,
                "orderType": order_type.value,
                "qtyAllOrd": "Y" if qty_all else "N",
            },
        )
        return CancelResult(
            ordNo=str(data.get("ordNo") or ""),
            krxFwdgOrgno=data.get("krxFwdgOrgno"),
            ordTime=data.get("ordTime"),
            action=action,
            msg=data.get("msg"),
        )

    async def get_unfilled(self) -> list[UnfilledOrder]:
        if self._paper is not None:
            return []   # 모의 즉시 체결 → 미체결 없음
        data = await self._post(
            "/api/kis/unfilled",
            {"account": self._cfg.account},
        )
        return [UnfilledOrder.model_validate(o) for o in data.get("orders", [])]

    async def get_orderable_amount(
        self,
        *,
        code: str | None = None,
        price: int = 0,
        order_type: OrderType = OrderType.LIMIT,
    ) -> OrderableAmount:
        if self._paper is not None:
            cash = self._paper.orderable_cash()
            max_qty = (cash // price) if price > 0 else 0
            return OrderableAmount(
                orderCashable=cash, maxBuyAmt=cash, maxBuyQty=int(max_qty),
            )
        data = await self._post(
            "/api/kis/inquire-psbl-order",
            {
                "account": self._cfg.account,
                "code": code or "",
                "price": price,
                "orderType": order_type.value,
            },
        )
        return OrderableAmount(
            orderCashable=data.get("orderCashable", 0),
            orderSubst=data.get("orderSubst", 0),
            reusableAmt=data.get("reusableAmt", 0),
            fundRcvableAmt=data.get("fundRcvableAmt", 0),
            maxBuyAmt=data.get("maxBuyAmt", 0),
            maxBuyQty=data.get("maxBuyQty", 0),
            cmaEvluAmt=data.get("cmaEvluAmt", 0),
        )

    # ─────────────────────────── 잔고 ───────────────────────────

    async def get_balance(self) -> BalanceSnapshot:
        if self._paper is not None:
            return await self._paper_balance()
        data = await self._post(
            "/api/kis/balance",
            {"account": self._cfg.account},
        )
        return BalanceSnapshot(
            cash=data["cash"],
            totalEval=data["totalEval"],
            totalPnl=data["totalPnl"],
            positions=data["positions"],
        )

    async def _paper_balance(self) -> BalanceSnapshot:
        """가상 잔고. 보유 종목은 실전 현재가로 평가손익을 산출한다."""
        assert self._paper is not None
        cash, positions = self._paper.snapshot()
        out: list[Position] = []
        total_eval = 0
        total_pnl = 0
        for code, pos in positions.items():
            cur = pos.avg_price
            try:
                cur = float((await self.get_price(code)).price) or pos.avg_price
            except Exception:  # noqa: BLE001 — 시세 실패 시 평단가로 평가
                log.debug("paper balance: price fetch failed for %s", code)
            eval_amt = int(cur * pos.qty)
            pnl = int((cur - pos.avg_price) * pos.qty)
            pct = ((cur - pos.avg_price) / pos.avg_price * 100) if pos.avg_price else 0.0
            total_eval += eval_amt
            total_pnl += pnl
            out.append(Position(
                code=code, name="", qty=pos.qty, avgPrice=int(pos.avg_price),
                currentPrice=int(cur), evalAmt=eval_amt, pnl=pnl,
                pnlPct=f"{pct:.2f}", loanDt="", crdtType="",
            ))
        return BalanceSnapshot(
            cash=int(cash), totalEval=int(cash) + total_eval,
            totalPnl=total_pnl, positions=out,
        )

    async def sync_credit_ledger(self) -> dict[str, str] | None:
        """현재 잔고의 신용 포지션을 ledger에 권위적으로 반영.

        - traidair는 ``loanDt``(KIS의 ``loan_dt``)를 응답에 포함한다.
        - 잔고에 없는 종목은 ledger에서 제거된다.
        - ``CreditLedger``가 주입되지 않은 경우 ``None`` 반환.
        """
        if self._paper is not None:
            return None   # 모의 모드는 신용 미사용
        if self._ledger is None:
            return None
        balance = await self.get_balance()
        return self._ledger.sync_from_balance(balance.positions)

    # ─────────────────────────── 매크로 / DART ───────────────────────────

    async def get_market_data(
        self,
        *,
        mode: str = "realtime",
        date: str | None = None,
        time: str | None = None,
        tf: int = 5,
    ) -> MarketDataResponse:
        """매크로 지수 (KOSPI/KOSDAQ/NASDAQ/VIX/USD-KRW/KOSPI200 등).

        mode='sim'은 paper 모드 전용 (CLAUDE.md §11).
        """
        if mode == "sim" and self._cfg.mode == Mode.LIVE:
            raise KisModeMismatchError("market-data sim mode is paper-only")
        params: dict[str, str] = {"mode": mode, "tf": str(tf)}
        if date:
            params["date"] = date
        if time:
            params["time"] = time
        data = await self._get("/api/market-data", params)
        return MarketDataResponse.model_validate(data)

    async def get_dart_list(
        self,
        *,
        days: int = 1,
        corp_code: str | None = None,
    ) -> DartListResponse:
        params: dict[str, str] = {"days": str(days)}
        if corp_code:
            params["corp_code"] = corp_code
        data = await self._get("/api/dart/list", params)
        return DartListResponse(
            status=data.get("status", "ok"),
            list=data.get("list", []),
            total=data.get("total", 0),
        )

    async def get_dart_corpcode(self, name: str) -> str | None:
        """traidair의 hardcoded 종목명 → DART 8자리 corp_code 매핑."""
        data = await self._get("/api/dart/corpcode", {"nm": name})
        code = data.get("corp_code")
        return code if code else None
