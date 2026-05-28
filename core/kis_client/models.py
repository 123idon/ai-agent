"""Pydantic v2 models for KIS responses through the traidair proxy.

CLAUDE.md §15.1 — ai-agent는 KIS App Key/Secret을 traidair에 위임하고,
응답은 모두 HTTP 200 + {ok, ...} / {ok:false, error} 형식이다.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


# ─────────────────────────── enums ───────────────────────────


class Mode(str, Enum):
    """ai-agent 내부 모드 (config/mode.yaml.current_mode)."""

    PAPER = "paper"
    LIVE = "live"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class CancelAction(str, Enum):
    CANCEL = "cancel"
    MODIFY = "modify"


# ─────────────────────────── auth ───────────────────────────


class TokenSlice(BaseModel):
    """traidair가 반환하는 토큰 앞 10자만 노출된 슬라이스."""

    token: str


# ─────────────────────────── chart ───────────────────────────


class ChartCandle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    t: str       # "HH:MM"
    date: str    # YYYYMMDD
    o: int
    h: int
    l: int
    c: int
    v: int
    isPrev: bool = False


class ChartResponse(BaseModel):
    code: str
    date: str
    prevDate: str | None = None
    tf: str
    candles: list[ChartCandle]
    prevCount: int = 0
    todayCount: int = 0


# ─────────────────────────── orderbook ───────────────────────────


class OrderbookLevel(BaseModel):
    price: int
    qty: int


class OrderbookSnapshot(BaseModel):
    asks: list[OrderbookLevel]
    bids: list[OrderbookLevel]
    totalAsk: int
    totalBid: int
    strength: float


# ─────────────────────────── price ───────────────────────────


class PriceSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str
    name: str
    price: int
    open: int
    high: int
    low: int
    volume: int
    change: int
    changePct: str


# ─────────────────────────── order / cancel ───────────────────────────


class OrderResult(BaseModel):
    ordNo: str
    krxFwdgOrgno: str | None = None
    ordTime: str | None = None
    msg: str | None = None


class CancelResult(OrderResult):
    action: CancelAction


# ─────────────────────────── unfilled ───────────────────────────


class UnfilledOrder(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ordNo: str
    orgOrdNo: str = ""
    krxFwdgOrgno: str = ""
    code: str
    name: str = ""
    side: Side
    ordQty: int
    filledQty: int = 0
    unfilledQty: int
    ordPrice: int = 0
    avgFilledPrice: int = 0
    ordTime: str = ""
    ordDvsn: str = ""
    ordDvsnName: str = ""
    rvseCnclName: str | None = None


# ─────────────────────────── orderable amount ───────────────────────────


class OrderableAmount(BaseModel):
    """매수가능액·신용 가용액 (HL-06 담보유지비율 검증용)."""

    orderCashable: int   # 주문가능현금
    orderSubst: int = 0  # 주문가능대용금
    reusableAmt: int = 0
    fundRcvableAmt: int = 0
    maxBuyAmt: int       # 최대매수금액 (현금 + 신용)
    maxBuyQty: int = 0
    cmaEvluAmt: int = 0


# ─────────────────────────── balance ───────────────────────────


class Position(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str
    name: str = ""
    qty: int
    avgPrice: int
    currentPrice: int
    evalAmt: int
    pnl: int
    pnlPct: str = "0.00"
    loanDt: str = ""        # 신용 매수일 YYYYMMDD (현금은 "")
    crdtType: str = ""      # 신용유형 (21=자기융자신규 등)


class BalanceSnapshot(BaseModel):
    cash: int
    totalEval: int
    totalPnl: int
    positions: list[Position]


# ─────────────────────────── volume-rank ───────────────────────────


class VolumeRankItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rank: int
    code: str
    name: str
    price: int
    change: int
    changePct: float
    volume: int
    turnover: int       # 거래대금 (원)
    volSurgePct: float
    volTurnoverPct: float
    listedShares: int = 0


class VolumeRankResponse(BaseModel):
    market: str
    rankBy: str
    items: list[VolumeRankItem]


# ─────────────────────────── investor ───────────────────────────


class InvestorPoint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    date: str
    close: int = 0
    change: int = 0
    foreignerQty: int = 0
    institutionQty: int = 0
    individualQty: int = 0
    foreignerAmt: int = 0   # 천원 단위
    institutionAmt: int = 0
    individualAmt: int = 0


class InvestorSeries(BaseModel):
    code: str
    series: list[InvestorPoint]


# ─────────────────────────── market-data ───────────────────────────


class MacroIndex(BaseModel):
    model_config = ConfigDict(extra="ignore")

    price: float | None = None
    prev: float | None = None
    chgPct: float | None = None
    lastUpdated: str | None = None
    lastUpdatedKST: str | None = None


class MarketDataResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: str
    indices: dict[str, MacroIndex]
    fetchedAt: str
    cutoffKST: str | None = None


# ─────────────────────────── DART ───────────────────────────


class DartItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    corp_code: str = ""
    corp_name: str = ""
    report_nm: str = ""
    rcept_no: str = ""
    rcept_dt: str = ""


class DartListResponse(BaseModel):
    status: str
    list: list[DartItem]
    total: int = 0
