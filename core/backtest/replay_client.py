"""ReplayKisClient — 룩어헤드 없는 과거 날짜 시세/주문 래퍼 (CLAUDE.md §17).

KisClient와 **동일한 메서드 시그니처**(duck-typed)를 제공한다. 에이전트는 모두
``KisClient`` 타입 힌트를 받지만 isinstance 검사를 하지 않으므로 본 클래스를 그대로
주입할 수 있다.

데이터 소스(모두 traidair 경유 — §15 헌법 준수, 유료 KRX 제거):
- 지수(코스피/코스닥)  : traidair ``market-data`` sim (Yahoo ^KS11/^KQ11 백엔드).
- 종목 데이터(유니버스): KIS ``volume-rank`` (traidair).
- 공시               : DART ``list``/``corpcode`` (traidair).

룩어헤드 방지 규칙:
- ``get_chart``       : KIS(traidair)에서 해당 거래일 분봉을 받아 **가상 시각 이전(<=)
                        분봉 + 전일 분봉**만 남긴다. 미래 분봉은 절대 노출하지 않는다.
- ``get_price``       : 잘린 분봉으로 현재가/시고저/거래량을 합성(실시간 시세 미사용).
- ``get_market_data`` : traidair sim 모드(date+time 컷오프)로 매크로 지수 산출(Yahoo).
- 주문/잔고          : ``PaperBroker`` 가상 체결(시장가는 합성 현재가로 체결).
- ``get_volume_rank`` : KIS 거래대금 상위(traidair). 단, KIS volume-rank는 실시간
                        라우트라 **유니버스 구성**은 '현재 시점' 상위 종목이다(유니버스
                        멤버십 한정 근사 — 종목별 가격/신호는 분봉 컷오프로 무(無)룩어헤드).
- ``get_dart_list``   : traidair DART는 '최근 N일' 라우트라 과거 일자 재현이 불가능 →
                        백테스트에서는 빈 목록(페널티 0). 과거 일자 공시는 traidair에
                        날짜 파라미터(bgn_de/end_de) 추가 시 활성화(§15.5).
"""
from __future__ import annotations

import logging
from typing import Any

from core.kis_client import (
    BalanceSnapshot,
    CancelAction,
    CancelResult,
    ChartCandle,
    ChartResponse,
    DartListResponse,
    KisBusinessError,
    KisClient,
    MacroIndex,
    MarketDataResponse,
    Mode,
    OrderableAmount,
    OrderbookLevel,
    OrderbookSnapshot,
    OrderResult,
    OrderType,
    Position,
    PriceSnapshot,
    Side,
    TokenSlice,
    UnfilledOrder,
    VolumeRankItem,
    VolumeRankResponse,
)
from core.kis_client.paper_broker import PaperBroker
from core.time_utils import SimClock, from_ymd, prev_business_day, ymd

log = logging.getLogger(__name__)


def _tick_size(price: int) -> int:
    """한국 주식 호가 단위 (hard_limits.tick_size와 동일 — core 레이어 독립용 복제)."""
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def _hhmm(t: str) -> str:
    """'09:01' / '0901' → '0901' (4자리 비교용)."""
    return "".join(ch for ch in str(t) if ch.isdigit())[:4].rjust(4, "0")


def _daily_candle_from_rows(date: str, rows: list[dict], *, is_prev: bool) -> ChartCandle:
    """하루치 분봉 row(dict)들을 1개의 일봉 ChartCandle로 집계."""
    return ChartCandle(
        t=date, date=date,
        o=int(rows[0]["o"]),
        h=max(int(r["h"]) for r in rows),
        l=min(int(r["l"]) for r in rows),
        c=int(rows[-1]["c"]),
        v=sum(int(r["v"]) for r in rows),
        isPrev=is_prev,
    )


def _daily_candle_from_candles(
    date: str, candles: list[ChartCandle], *, is_prev: bool,
) -> ChartCandle:
    """하루치 분봉 ChartCandle들을 1개의 일봉 ChartCandle로 집계(당일 컷오프용)."""
    return ChartCandle(
        t=date, date=date,
        o=int(candles[0].o),
        h=max(int(c.h) for c in candles),
        l=min(int(c.l) for c in candles),
        c=int(candles[-1].c),
        v=sum(int(c.v) for c in candles),
        isPrev=is_prev,
    )


class ReplayKisClient:
    def __init__(
        self,
        data_client: KisClient,
        clock: SimClock,
        broker: PaperBroker,
        *,
        account: str = "BACKTEST-01",
        candle_store: Any = None,
        names: dict[str, str] | None = None,
    ) -> None:
        self._data = data_client       # traidair (분봉·유니버스·매크로 폴백)
        self._clock = clock
        self._broker = broker
        self._account = account
        # 로컬 분봉 저장소(§18). 있으면 분봉/유니버스를 traidair 대신 로컬에서 사용.
        self._store = candle_store
        self._names = names or {}
        self._date = clock.date_str
        self._prev_dd: str | None = None
        # 같은 (code) 분봉을 같은 분 안에서 반복 호출하므로 일자 단위 캐시(전일+당일 원본).
        self._chart_cache: dict[str, ChartResponse] = {}
        # 컷오프(분) 단위 truncate 결과 캐시: 한 분 안에서 get_price/orderbook/balance/
        # signal/get_daily_chart 등이 같은 code 의 get_chart 를 여러 번 호출하는데, 매번
        # 전체 캔들 리스트를 재필터링하던 비용을 제거한다. code → (cutoff_hhmm, resp).
        self._trunc_cache: dict[str, tuple[str, ChartResponse]] = {}
        # 거래대금 랭킹(유니버스)은 전일 데이터 기반이라 세션 내 불변 → 세션당 1회 캐시.
        self._volrank_cache: dict[tuple, VolumeRankResponse] = {}
        # 직전 정상 매크로 스냅샷. 시장 데이터를 못 받은 분봉에서 직전 값으로 폴백해
        # 백테스트가 끊기지 않게 한다(못 받으면 GREEN 취급 = 빈 indices).
        self._last_market: MarketDataResponse | None = None
        # 로컬 basket 매크로 프록시 캐시(컷오프 분 단위). get_market_data 가 매 분봉마다
        # 호출되므로 같은 가상 분(hhmm) 안에서는 한 번만 계산한다(§21 완전 로컬·핫루프).
        self._macro_cache: dict[str, MarketDataResponse] = {}

    # ─────────────────────────── 세션 ───────────────────────────

    def set_session(self, date_str: str) -> None:
        """새 거래일 시작. 가상 일자 갱신 + 분봉/랭킹 캐시 초기화."""
        self._date = date_str
        self._prev_dd = None
        self._chart_cache.clear()
        self._trunc_cache.clear()
        self._volrank_cache.clear()
        self._macro_cache.clear()

    def _resolve_prev_dd(self) -> str | None:
        """직전 '데이터 보유' 영업일 (로컬 저장소 기준, 워밍업 분봉용)."""
        if self._prev_dd is not None:
            return self._prev_dd
        if self._store is None:
            return None
        cur = prev_business_day(from_ymd(self._date))
        for _ in range(8):
            ds = ymd(cur)
            if self._store.has_date(ds):
                self._prev_dd = ds
                return ds
            cur = prev_business_day(cur)
        self._prev_dd = ""
        return None

    @property
    def mode(self) -> Mode:
        return Mode.PAPER

    @property
    def simulate_orders(self) -> bool:
        return True

    @property
    def account(self) -> str:
        return self._account

    async def close(self) -> None:
        await self._data.close()

    async def __aenter__(self) -> "ReplayKisClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ─────────────────────────── 토큰 ───────────────────────────

    async def fetch_token(self) -> TokenSlice:
        return TokenSlice(token="SIM-BACKTE")  # 앞 10자만 노출 규약과 일관

    # ─────────────────────────── 분봉(룩어헤드 컷오프) ───────────────────────────

    async def _full_day_chart(self, code: str, tf: str) -> ChartResponse:
        if code in self._chart_cache:
            return self._chart_cache[code]
        if self._store is not None:
            resp = self._local_chart(code)
        else:
            resp = await self._data.get_chart(code, date=self._date, tf=tf)
        self._chart_cache[code] = resp
        return resp

    def _local_chart(self, code: str) -> ChartResponse:
        """로컬 저장소(§18)에서 전일+당일 분봉으로 ChartResponse 구성."""
        candles: list[ChartCandle] = []
        prev_dd = self._resolve_prev_dd()
        prev_n = 0
        if prev_dd:
            for r in self._store.read_symbol(prev_dd, code):
                candles.append(ChartCandle(
                    t=r["t"], date=str(r["date"]), o=int(r["o"]), h=int(r["h"]),
                    l=int(r["l"]), c=int(r["c"]), v=int(r["v"]), isPrev=True,
                ))
                prev_n += 1
        today = 0
        for r in self._store.read_symbol(self._date, code):
            candles.append(ChartCandle(
                t=r["t"], date=str(r["date"]), o=int(r["o"]), h=int(r["h"]),
                l=int(r["l"]), c=int(r["c"]), v=int(r["v"]), isPrev=False,
            ))
            today += 1
        return ChartResponse(
            code=code, date=self._date, prevDate=prev_dd, tf="1",
            candles=candles, prevCount=prev_n, todayCount=today,
        )

    def _truncate(self, candles: list[ChartCandle]) -> list[ChartCandle]:
        """전일 분봉 전체 + 당일 가상 시각 이전(<=) 분봉만 남긴다."""
        cutoff = self._clock.hhmm
        out: list[ChartCandle] = []
        for c in candles:
            if c.isPrev:
                out.append(c)
            elif c.date == self._date and _hhmm(c.t) <= cutoff:
                out.append(c)
        return out

    async def get_chart(
        self, code: str, *, date: str | None = None, tf: str = "1",
    ) -> ChartResponse:
        # 같은 분(cutoff) 안에서 같은 code 재호출은 캐시 반환(전체 캔들 재필터링 회피).
        cutoff = self._clock.hhmm
        cached = self._trunc_cache.get(code)
        if cached is not None and cached[0] == cutoff:
            return cached[1]
        full = await self._full_day_chart(code, tf)
        kept = self._truncate(list(full.candles))
        prev_n = sum(1 for c in kept if c.isPrev)
        resp = ChartResponse(
            code=full.code,
            date=self._date,
            prevDate=full.prevDate,
            tf=full.tf,
            candles=kept,
            prevCount=prev_n,
            todayCount=len(kept) - prev_n,
        )
        self._trunc_cache[code] = (cutoff, resp)
        return resp

    async def get_daily_chart(
        self, code: str, *, lookback: int = 40,
    ) -> ChartResponse:
        """일봉 게이트(§5.2)용 일봉 캔들. 로컬 저장소의 **과거 거래일별 분봉을 일봉으로
        집계**한다(룩어헤드 없음: 가상 일자 이전 날짜는 종일, 당일은 컷오프 분봉만).

        저장소가 없으면 빈 응답(분석부는 분봉만으로 판정). 당일 미완성 일봉은 컷오프
        시각까지의 분봉으로 합성해 마지막 캔들로 덧붙인다.
        """
        if self._store is None:
            return ChartResponse(
                code=code, date=self._date, prevDate=None, tf="D",
                candles=[], prevCount=0, todayCount=0,
            )
        dates = [d for d in self._store.available_dates() if d < self._date][-lookback:]
        candles: list[ChartCandle] = []
        for d in dates:
            # 영구 일봉 캐시(불변 과거) — parquet 재파싱 없이 집계 1캔들 조회.
            agg = self._store.daily_aggregate(d, code)
            if agg:
                candles.append(ChartCandle(
                    t=agg["t"], date=str(agg["date"]), o=int(agg["o"]),
                    h=int(agg["h"]), l=int(agg["l"]), c=int(agg["c"]),
                    v=int(agg["v"]), isPrev=True,
                ))
        # 당일 미완성 일봉(컷오프 분봉 집계).
        today = self._today_candles(list((await self.get_chart(code, tf="1")).candles))
        if today:
            candles.append(_daily_candle_from_candles(self._date, today, is_prev=False))
        prev_n = sum(1 for c in candles if c.isPrev)
        return ChartResponse(
            code=code, date=self._date,
            prevDate=dates[-1] if dates else None, tf="D",
            candles=candles, prevCount=prev_n, todayCount=len(candles) - prev_n,
        )

    def _today_candles(self, candles: list[ChartCandle]) -> list[ChartCandle]:
        return [c for c in candles if not c.isPrev and c.date == self._date]

    @staticmethod
    def _prev_close(candles: list[ChartCandle]) -> int:
        prevs = [c for c in candles if c.isPrev]
        if prevs:
            return int(prevs[-1].c)
        return int(candles[0].c) if candles else 0

    async def get_price(self, code: str) -> PriceSnapshot:
        chart = await self.get_chart(code, tf="1")
        candles = list(chart.candles)
        if not candles:
            raise KisBusinessError(f"no replay data for {code}", route="/api/kis/price")
        today = self._today_candles(candles)
        prev_close = self._prev_close(candles)
        if today:
            cur = int(today[-1].c)
            open_ = int(today[0].o)
            high = max(int(c.h) for c in today)
            low = min(int(c.l) for c in today)
            vol = sum(int(c.v) for c in today)
        else:
            cur = open_ = high = low = prev_close
            vol = 0
        change = cur - prev_close
        pct = (change / prev_close * 100) if prev_close else 0.0
        return PriceSnapshot(
            code=code, name="", price=cur, open=open_, high=high, low=low,
            volume=vol, change=change, changePct=f"{pct:.2f}",
        )

    async def get_orderbook(self, code: str) -> OrderbookSnapshot:
        """합성 호가창 (HL-05 슬리피지 검증용). 현재가 기준 5틱 사다리."""
        snap = await self.get_price(code)
        p = int(snap.price)
        tick = _tick_size(p)
        asks = [OrderbookLevel(price=p + tick * i, qty=1000) for i in range(10)]
        bids = [OrderbookLevel(price=max(tick, p - tick * i), qty=1000) for i in range(10)]
        return OrderbookSnapshot(
            asks=asks, bids=bids, totalAsk=10000, totalBid=10000, strength=100.0,
        )

    # ─────────────────────────── 주문/잔고 (PaperBroker) ───────────────────────────

    async def _fill(self, side: Side, code: str, qty: int, price: int) -> OrderResult:
        fill_price = float(price)
        if fill_price <= 0:
            fill_price = float((await self.get_price(code)).price)
        ord_no = self._broker.fill(side, code, qty, fill_price)
        return OrderResult(ordNo=ord_no, msg=f"BACKTEST FILL @{int(fill_price)}")

    async def place_order(
        self, *, side: Side, code: str, qty: int, price: int = 0,
        order_type: OrderType = OrderType.LIMIT,
    ) -> OrderResult:
        return await self._fill(side, code, qty, price)

    async def place_credit_order(
        self, *, side: Side, code: str, qty: int, price: int = 0,
        order_type: OrderType = OrderType.LIMIT,
        crdt_type: str | None = None, loan_date: str | None = None,
    ) -> OrderResult:
        # 백테스트는 신용도 현금 가상 체결로 처리(실주문 없음, §3.1과 동일 취지).
        return await self._fill(side, code, qty, price)

    async def cancel_order(
        self, *, org_ord_no: str, krx_fwdg_orgno: str,
        action: CancelAction = CancelAction.CANCEL, qty: int = 0, price: int = 0,
        order_type: OrderType = OrderType.LIMIT, qty_all: bool = True,
    ) -> CancelResult:
        return CancelResult(
            ordNo=org_ord_no, action=action, msg="BACKTEST no-op (instant fill)",
        )

    async def get_unfilled(self) -> list[UnfilledOrder]:
        return []

    async def get_orderable_amount(
        self, *, code: str | None = None, price: int = 0,
        order_type: OrderType = OrderType.LIMIT,
    ) -> OrderableAmount:
        cash = self._broker.orderable_cash()
        # 신용 포함 매수여력 = 가용현금 × 신용배수 (§1.1). 배수 1.0이면 현금 한도.
        mult = getattr(self._broker, "credit_multiplier", 1.0) or 1.0
        buy_power = int(cash * mult)
        max_qty = (buy_power // price) if price > 0 else 0
        return OrderableAmount(
            orderCashable=cash, maxBuyAmt=buy_power, maxBuyQty=int(max_qty),
        )

    async def get_balance(self) -> BalanceSnapshot:
        cash, positions = self._broker.snapshot()
        out: list[Position] = []
        total_eval = 0
        total_pnl = 0
        total_credit = 0.0
        for code, pos in positions.items():
            cur = pos.avg_price
            try:
                cur = float((await self.get_price(code)).price) or pos.avg_price
            except Exception:  # noqa: BLE001 — 시세 실패 시 평단가로 평가
                log.debug("replay balance: price fetch failed %s", code)
            eval_amt = int(cur * pos.qty)
            pnl = int((cur - pos.avg_price) * pos.qty)
            pct = ((cur - pos.avg_price) / pos.avg_price * 100) if pos.avg_price else 0.0
            total_eval += eval_amt
            total_pnl += pnl
            total_credit += pos.credit
            out.append(Position(
                code=code, name=self._names.get(code, ""), qty=pos.qty,
                avgPrice=int(pos.avg_price),
                currentPrice=int(cur), evalAmt=eval_amt, pnl=pnl,
                pnlPct=f"{pct:.2f}",
                loanDt="신용" if pos.credit > 0 else "", crdtType="",
            ))
        # 순자산(가상잔고) = 현금 + 보유평가 − 신용차입. 신용은 부채이므로 차감한다.
        return BalanceSnapshot(
            cash=int(cash), totalEval=int(cash) + total_eval - int(total_credit),
            totalPnl=total_pnl, positions=out,
        )

    async def sync_credit_ledger(self) -> dict[str, str] | None:
        return None   # 백테스트는 신용 미사용

    # ─────────────────────────── 스크리닝 유니버스 (KIS 거래대금) ───────────────────────────

    async def get_volume_rank(
        self, *, market: str = "0000", rank_by: int = 3,
        min_price: int | None = None, max_price: int | None = None, top_n: int = 30,
    ) -> VolumeRankResponse:
        """KIS 거래대금 상위(traidair) → 스크리닝 유니버스.

        로컬 저장소(§18)가 있으면 **직전 영업일** 로컬 분봉의 거래대금(Σ c·v) 상위로
        유니버스를 구성한다(장전 전일 데이터 → 룩어헤드 없음). 저장소가 없으면 KIS
        volume-rank(traidair, 실시간)로 폴백한다.
        """
        if self._store is None:
            return await self._data.get_volume_rank(
                market=market, rank_by=rank_by,
                min_price=min_price, max_price=max_price, top_n=top_n,
            )
        # 전일 거래대금 랭킹은 세션 내 불변 → 동일 파라미터 재계산 회피(세션당 1회).
        ck = (market, rank_by, min_price, max_price, top_n)
        hit = self._volrank_cache.get(ck)
        if hit is not None:
            return hit
        prev_dd = self._resolve_prev_dd()
        if not prev_dd:
            return VolumeRankResponse(market=market, rankBy=str(rank_by), items=[])
        ranked: list[tuple[int, int, int, str]] = []   # (turnover, last_close, vol, code)
        for code in self._store.symbols_on(prev_dd):
            if code.startswith("^"):
                continue   # 지수는 종목 유니버스에서 제외
            rows = self._store.read_symbol(prev_dd, code)
            if not rows:
                continue
            turnover = sum(int(r["c"]) * int(r["v"]) for r in rows)
            vol = sum(int(r["v"]) for r in rows)
            last_close = int(rows[-1]["c"])
            if last_close <= 0 or turnover <= 0:
                continue
            if min_price is not None and last_close < min_price:
                continue
            if max_price is not None and last_close > max_price:
                continue
            ranked.append((turnover, last_close, vol, code))
        ranked.sort(reverse=True)
        items = [
            VolumeRankItem(
                rank=i + 1, code=code, name=self._names.get(code, ""), price=last_close,
                change=0, changePct=0.0, volume=vol, turnover=turnover,
                volSurgePct=0.0, volTurnoverPct=0.0, listedShares=0,
            )
            for i, (turnover, last_close, vol, code) in enumerate(ranked[:top_n])
        ]
        resp = VolumeRankResponse(market=market, rankBy=str(rank_by), items=items)
        self._volrank_cache[ck] = resp
        return resp

    # ─────────────────────────── 매크로 (traidair sim 컷오프) ───────────────────────────

    async def get_market_data(
        self, *, mode: str = "realtime", date: str | None = None,
        time: str | None = None, tf: int = 5,
    ) -> MarketDataResponse:
        """매크로 지수. 백테스트에서는 **완전 로컬**로 산출한다(§21·§17).

        **최적화(§21) + 룩어헤드 차단(§17)**:
        1. 로컬 지수 분봉(^KS11/^KQ11/^IXIC)이 수집돼 있으면 그걸로 컷오프 산출.
        2. 없으면(키움 수집은 6자리 종목만 적재 → 지수 부재) **유니버스 구성종목 바스켓
           평균 등락률을 KOSPI 프록시**로 산출한다. 둘 다 컷오프(≤ 가상 시각) 분봉만 쓰므로
           룩어헤드가 없다.
        traidair sim 폴백은 **로컬 저장소가 아예 없을 때만**(라이브성 환경) 사용한다 —
        traidair sim 의 indices 는 과거 일자를 무시하고 *오늘 실시간* 값을 돌려줘(룩어헤드)
        백테스트엔 부적합하다.
        """
        sim_time = self._clock.now().strftime("%H:%M")
        if self._store is not None:
            cutoff = self._clock.hhmm
            cached = self._macro_cache.get(cutoff)
            if cached is not None:
                return cached
            idx: dict[str, MacroIndex] = {}
            for sym, key in (("^KS11", "kospi"), ("^KQ11", "kosdq"), ("^IXIC", "nasdaq")):
                try:
                    ch = await self.get_chart(sym, tf="1")
                except Exception:  # noqa: BLE001
                    continue
                today = [c for c in ch.candles if not c.isPrev]
                if not today:
                    continue
                price = float(today[-1].c)
                prevs = [c for c in ch.candles if c.isPrev]
                prev_close = float(prevs[-1].c) if prevs else price
                chg = ((price - prev_close) / prev_close * 100) if prev_close else 0.0
                idx[key] = MacroIndex(price=price, prev=prev_close, chgPct=round(chg, 2))
            # 지수 분봉이 없으면(키움 수집) 구성종목 바스켓 평균으로 KOSPI 프록시 산출.
            if "kospi" not in idx:
                proxy = await self._basket_macro_proxy()
                if proxy is not None:
                    idx["kospi"] = proxy
                    idx.setdefault("kosdq", proxy)
            resp = MarketDataResponse(
                mode="sim", indices=idx,
                fetchedAt=self._clock.now().isoformat(), cutoffKST=sim_time,
            )
            self._macro_cache[cutoff] = resp
            if idx:
                self._last_market = resp
            return resp
        try:
            resp = await self._data.get_market_data(
                mode="sim", date=self._date, time=sim_time, tf=tf,
            )
            # 빈 indices(traidair가 데이터 못 구한 경우)면 직전 정상값을 우선 유지.
            if resp.indices:
                self._last_market = resp
                return resp
            if self._last_market is not None:
                return self._last_market
            return resp
        except Exception as e:  # noqa: BLE001
            # 어떤 에러(검증/네트워크/타임아웃)여도 백테스트를 멈추지 않는다 —
            # 직전 정상 스냅샷이 있으면 그대로, 없으면 빈 지수(GREEN)로 계속 진행.
            log.warning("get_market_data sim 실패(%s) — 직전 값/GREEN 폴백", e)
            if self._last_market is not None:
                return self._last_market
            return MarketDataResponse(
                mode="sim", indices={}, fetchedAt=self._clock.now().isoformat(),
                cutoffKST=sim_time,
            )

    # 바스켓→지수 베타 감쇠: 거래대금 상위(고베타 대형주) 바스켓은 실제 KOSPI 지수보다
    # 변동을 과장한다(예: 지수 -0.5%인데 바스켓 -1.5%). 등급 임계(YELLOW -0.8%/RED -1.5%)는
    # 실제 지수 기준이므로, 바스켓 등락률에 베타 계수를 곱해 지수 수준으로 낮춘다.
    _BASKET_BETA = 0.65

    async def _basket_macro_proxy(self) -> MacroIndex | None:
        """지수 분봉이 없을 때 유니버스 구성종목 바스켓으로 KOSPI 프록시 등락률을 산출.

        전일 거래대금 상위(KOSPI200/KOSDAQ150 대형주 중심, get_volume_rank 세션 캐시)
        종목들의 **컷오프 분봉** 기준 (당일 현재가 vs 전일 종가) 등락률을 모아 **중앙값**
        (이상치에 강건)을 취하고, **베타 감쇠(`_BASKET_BETA`)**로 고베타 과장을 지수 수준으로
        낮춘다 — top-40 단순평균은 그날 큰 변동 종목(거래대금 상위=대형 무버)에 끌려 시장을
        실제보다 약세(RED 과다)로 읽어 진입을 과차단했다(§21 부작용 수정). 컷오프 분봉만
        쓰므로 룩어헤드 없음(§17). 등락률만 등급 판정에 쓰여 price/prev 는 합성값.
        표본 없으면 None(→ 빈 지수 = GREEN, §19 무거래 자가정지 방지와 일관).
        """
        try:
            uni = await self.get_volume_rank(top_n=60)
        except Exception:  # noqa: BLE001
            return None
        chgs: list[float] = []
        for it in uni.items:
            code = it.code
            if code.startswith("^"):
                continue
            try:
                ch = await self.get_chart(code, tf="1")
            except Exception:  # noqa: BLE001
                continue
            today = [c for c in ch.candles if not c.isPrev]
            prevs = [c for c in ch.candles if c.isPrev]
            if not today or not prevs:
                continue
            price = float(today[-1].c)
            prev_close = float(prevs[-1].c)
            if prev_close <= 0:
                continue
            chgs.append((price - prev_close) / prev_close * 100.0)
        if not chgs:
            return None
        chgs.sort()
        n = len(chgs)
        median = chgs[n // 2] if n % 2 else (chgs[n // 2 - 1] + chgs[n // 2]) / 2.0
        chg = median * self._BASKET_BETA   # 고베타 바스켓 → 지수 수준 감쇠
        # 합성 지수: prev=1000 기준, 등락률을 그대로 반영(등급 판정은 chgPct 만 사용).
        prev = 1000.0
        return MacroIndex(price=round(prev * (1 + chg / 100.0), 2), prev=prev, chgPct=round(chg, 2))

    # ─────────────────────────── DART ───────────────────────────

    async def get_dart_list(
        self, *, days: int = 1, corp_code: str | None = None,
    ) -> DartListResponse:
        # traidair DART는 '최근 N일' 라우트라 과거 일자 재현 불가 → 룩어헤드 방지 위해 빈 목록.
        return DartListResponse(status="ok", list=[], total=0)

    async def get_dart_corpcode(self, name: str) -> str | None:
        try:
            return await self._data.get_dart_corpcode(name)
        except Exception:  # noqa: BLE001
            return None
