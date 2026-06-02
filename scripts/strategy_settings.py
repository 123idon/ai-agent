"""HTS ⚙️ 매매 설정 패널용 현재값/안전범위 조회 (CLAUDE.md §5.3~5.5, §24).

traidair HTS '⚙️ 매매 설정' 패널이 페이지 로드/새로고침 시 호출해, ``strategy_params.yaml``
의 **현재 적용값**과 각 항목의 **안전범위(가드레일)**, **모드(잠금 여부)**를 한 번에 돌려준다.
값 변경(저장)은 기존 ``consult_apply.py <key> <value>`` 경로(StrategyEditor 단일 진입점:
화이트리스트 → leaf 교체 → 재읽기 검증 → git 커밋 → ImprovementLog)를 그대로 재사용한다.

읽기 전용 — 파일을 수정하지 않는다. 결과 JSON 을 stdout 한 줄로 출력(traidair 파싱).

usage:
  python scripts/strategy_settings.py --get
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

import yaml  # noqa: E402

from agents.meta.optimizer.main import TUNABLE_KEYS  # noqa: E402
from core.strategy.editor import (  # noqa: E402
    TUNE_BOUNDS_LIST,
    TUNE_BOUNDS_SCALAR,
)

ROOT = Path(__file__).parents[1]
CONFIG_PATH = ROOT / "config" / "strategy_params.yaml"
HARD_LIMITS_PATH = ROOT / "config" / "hard_limits.yaml"
MODE_PATH = ROOT / "config" / "mode.yaml"

# HTS 패널이 노출하는 키 목록. (UI 항목 순서대로)
#   kind: "pct"  → yaml 은 소수(0.02), 화면은 % (2%) — UI 가 ×100/÷100 변환
#         "pct_range" → [low, high] 소수 리스트(±%) — UI 가 변환
#         "minutes"/"ratio_pct"/"num" → 그대로 숫자, "bool"/"choice" → 그대로
PANEL_KEYS: list[dict] = [
    # ── 손절 ──
    {"key": "stop_loss.hard_max_pct", "kind": "pct"},
    {"key": "stop_loss.technical_stop_enabled", "kind": "bool"},
    {"key": "stop_loss.technical_buffer_pct", "kind": "pct"},
    # ── 타임스톱: 제거됨 (시간 기반 매도 폐지, §5.5) — 패널에 노출하지 않는다. ──
    # ── 익절 ──
    {"key": "take_profit.step1.pct_range", "kind": "pct_range"},
    {"key": "take_profit.step1.close_ratio", "kind": "ratio_pct"},
    {"key": "take_profit.step2.pct_range", "kind": "pct_range"},
    {"key": "take_profit.step2.close_ratio", "kind": "ratio_pct"},
    {"key": "take_profit.step3_trailing.trail_from_high_pct", "kind": "pct"},
]


def _emit(data: dict) -> int:
    sys.stdout.write(json.dumps(data, ensure_ascii=False))
    sys.stdout.flush()
    return 0


def _yaml_value(doc, dotted_key: str):
    node = doc
    for seg in dotted_key.split("."):
        if not isinstance(node, dict) or seg not in node:
            return None
        node = node[seg]
    return node


def _mode() -> str:
    try:
        raw = MODE_PATH.read_text(encoding="utf-8")
        doc = yaml.safe_load(raw) or {}
        m = doc.get("current_mode")
        if isinstance(m, str) and m.strip():
            return m.strip().lower()
    except Exception:  # noqa: BLE001
        pass
    return "paper"


def main() -> int:
    parser = argparse.ArgumentParser(description="HTS 매매 설정 현재값/범위 조회")
    parser.add_argument("--get", action="store_true", help="현재값+범위 조회")
    parser.parse_args()

    try:
        doc = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        return _emit({"ok": False, "reason": f"설정 파일 읽기 실패: {exc}"[:200]})

    mode = _mode()
    locked = mode != "paper"

    items = []
    for spec in PANEL_KEYS:
        key = spec["key"]
        item = {
            "key": key,
            "kind": spec["kind"],
            "value": _yaml_value(doc, key),
            "tunable": key in TUNABLE_KEYS,   # 화이트리스트(저장 가능)
        }
        if "choices" in spec:
            item["choices"] = spec["choices"]
        # 안전범위(가드레일) — StrategyEditor 가 자동 보정하는 경계.
        if key in TUNE_BOUNDS_SCALAR:
            lo, hi = TUNE_BOUNDS_SCALAR[key]
            item["bounds"] = [lo, hi]
        if key in TUNE_BOUNDS_LIST:
            item["list_bounds"] = [list(b) for b in TUNE_BOUNDS_LIST[key]]
        items.append(item)

    return _emit({
        "ok": True,
        "mode": mode,
        "locked": locked,
        "items": items,
    })


if __name__ == "__main__":
    sys.exit(main())
