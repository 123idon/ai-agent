"""Boot all agents in live mode (CLAUDE.md §9)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from core.kis_client import Mode
from scripts.run_paper import _run


def main() -> int:
    return asyncio.run(_run(Mode.LIVE))


if __name__ == "__main__":
    sys.exit(main())
