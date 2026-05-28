"""Probe basic configuration & infrastructure readiness."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from agents.analysis.signal.indicators import SignalParams
from agents.intel.screening.main import ScreeningParams
from agents.risk.risk_manager.hard_limits import HardLimitsConfig
from core.kis_client import KisClientConfig


def main() -> int:
    root = Path(__file__).parents[1]
    fails: list[str] = []

    def check(label: str, fn) -> None:
        try:
            fn()
            print(f"[OK]   {label}")
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {label}: {e}")
            fails.append(label)

    check("kis_api.yaml (KisClientConfig.from_files)",
          lambda: KisClientConfig.from_files(project_root=root))
    check("hard_limits.yaml (HardLimitsConfig.from_file)",
          lambda: HardLimitsConfig.from_file(root / "config" / "hard_limits.yaml"))
    check("strategy_params.yaml (SignalParams.from_file)",
          lambda: SignalParams.from_file(root / "config" / "strategy_params.yaml"))
    check("strategy_params.yaml (ScreeningParams.from_file)",
          lambda: ScreeningParams.from_file(root / "config" / "strategy_params.yaml"))

    if fails:
        print(f"\n{len(fails)} check(s) failed: {', '.join(fails)}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
