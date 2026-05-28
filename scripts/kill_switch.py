"""Emergency stop — writes a sentinel file the running CEO polls (CLAUDE.md §10)."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).parents[1]
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    sentinel = state / "KILL_SWITCH"
    sentinel.touch()
    print(f"kill switch sentinel written: {sentinel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
