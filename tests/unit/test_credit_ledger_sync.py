"""Tests for CreditLedger.sync_from_balance + KisClient.sync_credit_ledger."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import httpx

from core.kis_client import (
    CreditLedger,
    KisClient,
    KisClientConfig,
    Mode,
)


def _kis(
    handler: Callable[[httpx.Request], httpx.Response],
    ledger: CreditLedger | None = None,
    *,
    mode: Mode = Mode.LIVE,
) -> KisClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://traidair.test", transport=transport,
        timeout=httpx.Timeout(6.0),
    )
    cfg = KisClientConfig(
        base_url="http://traidair.test", app_key="AK", app_secret="AS",
        account="12345678-01", mode=mode,
    )
    return KisClient(cfg, credit_ledger=ledger, http_client=http)


def test_sync_overwrites_with_authoritative_loan_dt(tmp_path: Path) -> None:
    """KIS의 loanDt가 ledger의 잠정 값을 덮어쓴다."""
    ledger = CreditLedger(tmp_path / "ledger.json")
    ledger.record_buy("005930")  # 잠정: 오늘 날짜
    initial = ledger.loan_dt("005930")
    assert initial is not None

    class _Pos:
        code = "005930"
        qty = 10
        loanDt = "20260501"     # 권위적 값

    ledger.sync_from_balance([_Pos()])
    assert ledger.loan_dt("005930") == "20260501"


def test_sync_preserves_when_balance_has_no_loan_dt(tmp_path: Path) -> None:
    """잔고에 loanDt가 비어있어도 ledger의 기존 값을 유지."""
    ledger = CreditLedger(tmp_path / "ledger.json")
    ledger.record_buy("005930")
    initial = ledger.loan_dt("005930")

    class _Pos:
        code = "005930"
        qty = 10
        loanDt = ""

    ledger.sync_from_balance([_Pos()])
    assert ledger.loan_dt("005930") == initial


def test_sync_removes_codes_absent_from_balance(tmp_path: Path) -> None:
    """잔고에 없는 종목은 ledger에서 제거 (포지션 청산 완료)."""
    ledger = CreditLedger(tmp_path / "ledger.json")
    ledger.record_buy("005930")
    ledger.record_buy("000660")
    assert ledger.loan_dt("000660") is not None

    class _Pos:
        code = "005930"
        qty = 10
        loanDt = "20260501"

    ledger.sync_from_balance([_Pos()])
    assert ledger.loan_dt("005930") == "20260501"
    assert ledger.loan_dt("000660") is None


async def test_kisclient_sync_credit_ledger_uses_balance(tmp_path: Path) -> None:
    ledger = CreditLedger(tmp_path / "ledger.json")
    ledger.record_buy("005930")

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/kis/balance"
        return httpx.Response(200, json={
            "ok": True, "cash": 0, "totalEval": 0, "totalPnl": 0,
            "positions": [{
                "code": "005930", "name": "삼성전자", "qty": 10,
                "avgPrice": 70000, "currentPrice": 71000, "evalAmt": 710000,
                "pnl": 10000, "pnlPct": "1.43",
                "loanDt": "20260520", "crdtType": "21",
            }],
        })

    async with _kis(handler, ledger) as kc:
        result = await kc.sync_credit_ledger()
    assert result == {"005930": "20260520"}
    assert ledger.loan_dt("005930") == "20260520"


async def test_kisclient_sync_returns_none_without_ledger() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no balance call expected")

    async with _kis(handler, ledger=None) as kc:
        result = await kc.sync_credit_ledger()
    assert result is None
