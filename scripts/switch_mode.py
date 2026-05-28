"""Switch between paper and live modes (CLAUDE.md §3)."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

_KST = timezone(timedelta(hours=9))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", choices=("paper", "live"), required=True)
    args = parser.parse_args()

    root = Path(__file__).parents[1]
    mode_path = root / "config" / "mode.yaml"
    if not mode_path.exists():
        print(f"mode.yaml not found at {mode_path}", file=sys.stderr)
        return 1

    doc = yaml.safe_load(mode_path.read_text(encoding="utf-8")) or {}
    current = doc.get("current_mode", "paper")
    if current == args.to:
        print(f"already in {args.to} mode")
        return 0

    doc["current_mode"] = args.to
    doc["lock"] = {
        "strategy_params_sha256": None,
        "locked_at": None,
        "locked_by": None,
    }
    history = doc.setdefault("history", [])
    history.append({
        "from": current, "to": args.to,
        "at": datetime.now(_KST).isoformat(),
        "by": "switch_mode.py",
    })

    mode_path.write_text(
        yaml.safe_dump(doc, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    print(f"switched: {current} -> {args.to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
