"""Unit tests for scripts/apply_proposal.py (제안 적용 + 검수, §2.7/§3.3).

검수 비교 로직(``_same``)·yaml 읽기(``_yaml_value``)와, 파일을 건드리지 않는
방어 분기(빈 id / 결과 없음 / 미존재 제안)를 검증한다. 실제 파일 수정 + git 커밋
경로는 통합(수동) 검증으로 다룬다 — 여기서는 부수효과 없는 경로만 단위 검증.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
STRAT = ROOT / "config" / "strategy_params.yaml"

_spec = importlib.util.spec_from_file_location(
    "apply_proposal", ROOT / "scripts" / "apply_proposal.py",
)
ap = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(ap)


# ─────────────────────────── 검수 비교 로직 ───────────────────────────


def test_same_handles_int_float_equivalence() -> None:
    assert ap._same(30, 30.0) is True
    assert ap._same(0.005, 0.005) is True
    assert ap._same(0.005, 0.0050000001) is True   # 부동소수 허용
    assert ap._same(30, 25) is False
    assert ap._same("a", "a") is True
    assert ap._same("a", "b") is False


def test_yaml_value_reads_nested_scalar() -> None:
    # 가변 config 값을 하드코딩하지 않는다(consult/auto-learn 이 수시로 바꿈) — 같은 파일을
    # 독립 파싱한 기대값과 일치하는지(=중첩 키 탐색이 정확한지)만 검증한다.
    import yaml
    doc = yaml.safe_load(STRAT.read_text(encoding="utf-8"))
    assert ap._yaml_value(STRAT, "stop_loss.hard_max_pct") \
        == doc["stop_loss"]["hard_max_pct"]
    assert ap._yaml_value(STRAT, "screening.threshold") == doc["screening"]["threshold"]
    assert ap._yaml_value(STRAT, "does.not.exist") is None


# ─────────────────────────── 방어 분기 (부수효과 없음) ───────────────────────────


def test_empty_id_rejected(capsys) -> None:
    rc = ap.main.__wrapped__ if hasattr(ap.main, "__wrapped__") else None
    # main()은 argv/env에서 id를 읽는다 → 빈 id 직접 호출.
    import sys
    old = sys.argv
    sys.argv = ["apply_proposal.py", ""]
    try:
        ap.main()
    finally:
        sys.argv = old
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "proposal_id" in out["reason"] or "비어" in out["reason"]


def test_unknown_proposal_id_rejected(tmp_path, capsys, monkeypatch) -> None:
    # 실제 evolve_result.json을 건드리지 않도록, 존재하지 않을 id를 조회.
    import sys
    old = sys.argv
    sys.argv = ["apply_proposal.py", "definitely_not_a_real_id_zzz"]
    try:
        ap.main()
    finally:
        sys.argv = old
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    # paper 모드면 '찾을 수 없'/'진화 결과가 없' 둘 중 하나(결과 파일 유무에 따라).
    assert ("찾을 수 없" in out["reason"]) or ("진화 결과가 없" in out["reason"]) \
        or out.get("locked") is True
