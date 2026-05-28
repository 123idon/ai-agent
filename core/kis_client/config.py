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
        mode_block = kis_doc.get(current_mode.value)
        if not mode_block:
            raise KisConfigError(f"missing '{current_mode.value}' section in kis_api.yaml")

        base_url = (
            kis_doc.get("traidair_base_url")
            or os.getenv("TRAIDAIR_BASE_URL")
            or _DEFAULT_BASE_URL
        ).rstrip("/")

        return cls(
            base_url=base_url,
            app_key=mode_block["app_key"],
            app_secret=mode_block["app_secret"],
            account=mode_block["account_no"],
            mode=current_mode,
        )

    @property
    def traidair_mode(self) -> str:
        """traidair가 받는 모드 값: paper→mock, live→real."""
        return "real" if self.mode == Mode.LIVE else "mock"
