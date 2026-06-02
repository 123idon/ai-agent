"""KIS client configuration loaded from config/mode.yaml + config/kis_api.yaml.

CLAUDE.md §3 — current_mode 결정과 키 로드를 한 곳에서 처리.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .exceptions import KisConfigError
from .models import Mode

_DEFAULT_BASE_URL = "http://localhost:3000"


def _is_placeholder(value: object) -> bool:
    """비어있거나 예시/플레이스홀더로 보이는 키 값인지."""
    if not value:
        return True
    s = str(value)
    if len(s) < 10:
        return True
    return "TODO" in s or "여기" in s or set(s) <= {"0", "-"}


@dataclass(frozen=True)
class KisClientConfig:
    base_url: str            # traidair endpoint
    app_key: str
    app_secret: str
    account: str             # "########-##"
    mode: Mode
    timeout_seconds: float = 6.0
    retry_initial_backoff_ms: int = 200
    retry_max_backoff_ms: int = 800
    # paper(모의) 모드: 시세/지표는 실전 키로 실제 수신, 주문만 로컬 가상 시뮬레이션.
    simulate_orders: bool = False
    paper_start_cash: int = 100_000_000
    # ai-agent ↔ traidair 통합 API(/api/agent/*) 인증 키 (CLAUDE.md §22, X-Agent-Key 헤더).
    agent_key: str = "traidair-agent-dev"

    @classmethod
    def from_files(
        cls,
        *,
        project_root: Path,
        mode_override: Mode | None = None,
    ) -> "KisClientConfig":
        mode_path = project_root / "config" / "mode.yaml"
        kis_path = project_root / "config" / "kis_api.yaml"
        if not mode_path.exists():
            raise KisConfigError(f"mode.yaml not found at {mode_path}")
        if not kis_path.exists():
            raise KisConfigError(
                f"kis_api.yaml not found at {kis_path}. "
                f"Copy kis_api.yaml.example and fill in values."
            )

        mode_doc = yaml.safe_load(mode_path.read_text(encoding="utf-8"))
        kis_doc = yaml.safe_load(kis_path.read_text(encoding="utf-8"))

        current_mode = mode_override or Mode(mode_doc["current_mode"])
        mode_block = kis_doc.get(current_mode.value) or {}
        live_block = kis_doc.get("live") or {}

        app_key = mode_block.get("app_key")
        app_secret = mode_block.get("app_secret")
        account = mode_block.get("account_no", "")

        # 실전 키만 보유한 경우: paper 키/계좌가 비었거나 placeholder면 live 값으로 폴백.
        # (paper 모드에서도 데이터는 실전 키로 받고, 주문은 시뮬레이션하므로 안전)
        if current_mode == Mode.PAPER:
            if _is_placeholder(app_key):
                app_key = live_block.get("app_key", app_key)
            if _is_placeholder(app_secret):
                app_secret = live_block.get("app_secret", app_secret)
            if _is_placeholder(account):
                account = live_block.get("account_no", account)

        if _is_placeholder(app_key) or _is_placeholder(app_secret):
            raise KisConfigError(
                f"'{current_mode.value}' 섹션(또는 live 폴백)에 유효한 app_key/app_secret 필요"
            )

        base_url = (
            kis_doc.get("traidair_base_url")
            or os.getenv("TRAIDAIR_BASE_URL")
            or _DEFAULT_BASE_URL
        ).rstrip("/")

        start_cash = int(
            kis_doc.get("paper_start_cash")
            or (kis_doc.get("paper") or {}).get("start_cash")
            or 100_000_000
        )

        agent_key = (
            kis_doc.get("agent_key")
            or os.getenv("TRAIDAIR_AGENT_KEY")
            or "traidair-agent-dev"
        )

        return cls(
            base_url=base_url,
            app_key=app_key,
            app_secret=app_secret,
            account=account,
            mode=current_mode,
            simulate_orders=(current_mode == Mode.PAPER),
            paper_start_cash=start_cash,
            agent_key=agent_key,
        )

    @property
    def traidair_mode(self) -> str:
        """traidair가 받는 모드 값.

        시세/지표/토큰은 항상 실전 호스트(real)를 사용한다. paper 모드에서도
        실전 키로 실제 데이터를 받으며, 주문 실행만 ``simulate_orders``로 로컬
        가상 처리한다(CLAUDE.md §3.1, §15.2).
        """
        return "real"
