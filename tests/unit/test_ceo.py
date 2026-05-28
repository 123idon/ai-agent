"""Unit tests for CeoAgent."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Callable

import httpx

from agents.ceo.main import CeoAgent, KILL_SENTINEL, StrategyLockBroken
from core.kis_client import KisClient, KisClientConfig, Mode
from core.messaging import Bus


def _kis(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    mode: Mode = Mode.PAPER,
) -> KisClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url="http://traidair.test", transport=transport,
        timeout=httpx.Timeout(6.0),
    )
    cfg = KisClientConfig(
        base_url="http://traidair.test",
        app_key="AK", app_secret="AS",
        account="12345678-01", mode=mode,
    )
    return KisClient(cfg, http_client=http)


def _bootstrap_handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/api/kis/token":
        return httpx.Response(200, json={"ok": True, "token": "abc1234..."})
    if req.url.path == "/api/kis/balance":
        return httpx.Response(200, json={
            "ok": True, "cash": 100_000_000, "totalEval": 100_000_000,
            "totalPnl": 0, "positions": [],
        })
    raise AssertionError(req.url.path)


def _make_project(tmp_path: Path) -> Path:
    """Minimal project layout for CeoAgent."""
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "strategy_params.yaml").write_text(
        "signal: {}\n", encoding="utf-8",
    )
    return tmp_path


async def test_bootstrap_in_paper_does_not_capture_hash(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    async with _kis(_bootstrap_handler) as kc:
        ceo = CeoAgent(kc, Bus(), project_root=root)
        await ceo.bootstrap()
        assert ceo.verify_strategy_lock()  # paper는 항상 True


async def test_bootstrap_in_live_captures_hash(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    async with _kis(_bootstrap_handler, mode=Mode.LIVE) as kc:
        ceo = CeoAgent(kc, Bus(), project_root=root)
        await ceo.bootstrap()
        assert ceo.verify_strategy_lock()
        # 파일 변경 → 해시 불일치 감지
        (root / "config" / "strategy_params.yaml").write_text(
            "signal: {tampered: true}\n", encoding="utf-8",
        )
        assert not ceo.verify_strategy_lock()


async def test_kill_switch_sentinel_stops_run_forever(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    async with _kis(_bootstrap_handler) as kc:
        ceo = CeoAgent(kc, Bus(), project_root=root)
        await ceo.bootstrap()

        async def trigger() -> None:
            await asyncio.sleep(0.1)
            (root / "state" / KILL_SENTINEL).touch()

        await asyncio.gather(
            ceo.run_forever(poll_interval=0.05),
            trigger(),
        )
        assert ceo.stop_event.is_set()


async def test_kill_method_stops_run_forever(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    async with _kis(_bootstrap_handler) as kc:
        ceo = CeoAgent(kc, Bus(), project_root=root)
        await ceo.bootstrap()

        async def trigger() -> None:
            await asyncio.sleep(0.1)
            ceo.kill(reason="test")

        await asyncio.gather(
            ceo.run_forever(poll_interval=0.05),
            trigger(),
        )
        assert ceo.stop_event.is_set()
