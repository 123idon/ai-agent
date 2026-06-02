"""복기 → 학습 → 자동 반영 파이프라인 (CLAUDE.md §2.6, §11).

매매 종료 후(또는 백테스트 마감) 저널을 집계해 반복 손실 패턴에서 개선안을 뽑고,
**paper 모드에서만** ``strategy_params.yaml`` 에 자동 반영한다. 모든 반영은
``ImprovementLog`` 에 누적되고(세션 간 기억), 변경 전후 성과를 비교해 효과 없는
변경은 롤백 후보로 표시한다.

흐름:
  1. 최근 N 거래일 저널 로드(``data/journal/*.jsonl``)
  2. ``ReviewLearner`` 로 개선안 추출(손절 연속 등; 타임스톱은 폐지됨 §5.5)
  3. ``StrategyEditor`` 로 화이트리스트 키 자동 반영(+git 커밋 +기록)
  4. ``ImprovementLog.evaluate_effects`` 로 전후 성과 비교 → 롤백 후보 산출
  5. 결과를 ``state/learn_result.json`` 에 저장(HTS '최근 적용 변경사항' 패널) + stdout JSON

usage:
  python scripts/auto_learn.py [--days N] [--dry-run] [--apply/--no-apply]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

import yaml  # noqa: E402

from core.kis_client import KisClientConfig, Mode  # noqa: E402
from core.learning import ReviewLearner  # noqa: E402
from core.memory import ImprovementLog  # noqa: E402
from core.strategy import StrategyEditor  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parents[1]
MEMORY_DIR = ROOT / "data" / "memory"
JOURNAL_DIR = ROOT / "data" / "journal"
CONFIG_PATH = ROOT / "config" / "strategy_params.yaml"
STATE_PATH = ROOT / "state" / "learn_result.json"


def _emit(data: dict) -> int:
    sys.stdout.write(json.dumps(data, ensure_ascii=False))
    sys.stdout.flush()
    return 0


def _recent_records(days: int) -> list[dict[str, Any]]:
    files = sorted(JOURNAL_DIR.glob("*.jsonl"), key=lambda p: p.stem)[-days:]
    out: list[dict[str, Any]] = []
    for p in files:
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            continue
    return out


def _current_value(dotted_key: str) -> Any:
    doc = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    node: Any = doc
    for seg in dotted_key.split("."):
        if not isinstance(node, dict) or seg not in node:
            return None
        node = node[seg]
    return node


def main() -> int:
    parser = argparse.ArgumentParser(description="복기 → 학습 → 자동 반영")
    parser.add_argument("--days", type=int, default=3, help="집계할 최근 거래일 수")
    parser.add_argument("--dry-run", action="store_true", help="추출만, 적용 안 함")
    args = parser.parse_args()

    now = datetime.now(KST)
    ts, date = now.isoformat(), now.strftime("%Y%m%d")

    try:
        cfg = KisClientConfig.from_files(project_root=ROOT)
        mode = cfg.mode
    except Exception as exc:  # noqa: BLE001
        return _emit({"ok": False, "reason": f"설정 로드 실패: {exc}"[:200]})

    records = _recent_records(args.days)
    learner = ReviewLearner(_current_value)
    suggestions = learner.analyze(records)

    applied: list[dict] = []
    skipped: list[dict] = []
    if not args.dry_run and mode == Mode.PAPER:
        editor = StrategyEditor(
            config_path=CONFIG_PATH, memory_dir=MEMORY_DIR, project_root=ROOT,
            mode=mode.value,
        )
        for s in suggestions:
            res = editor.apply(
                s.key, s.to_value, ts=ts, date=date, source="review",
                reason=s.reason, expected_effect=s.expected_effect,
            )
            (applied if res.ok else skipped).append(res.to_dict())
    else:
        skipped = [
            {"key": s.key, "from": s.from_value, "to": s.to_value, "reason": s.reason}
            for s in suggestions
        ]

    # 효과 평가 + 롤백 후보
    imp = ImprovementLog.load(MEMORY_DIR)
    imp.evaluate_effects(JOURNAL_DIR)
    rollback = imp.rollback_candidates()

    result = {
        "ok": True,
        "mode": mode.value,
        "ts": ts,
        "suggestions": [
            {"key": s.key, "from": s.from_value, "to": s.to_value,
             "reason": s.reason, "expected_effect": s.expected_effect,
             "evidence": s.evidence}
            for s in suggestions
        ],
        "applied": applied,
        "skipped": skipped,
        "rollback_candidates": rollback,
        "timeline": imp.timeline(limit=10),
        "locked": mode != Mode.PAPER,
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return _emit(result)


if __name__ == "__main__":
    sys.exit(main())
