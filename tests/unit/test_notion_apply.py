"""Unit tests — 노션 학습 적용 (scripts/notion_apply.py, §23·§24.5).

검증: 키→부서 매핑 · 값 coercion · 동기화 안 됨 안내 · 추출(적용가능/하드리밋/동일값/
미반영 pending) · 실제 적용(yaml 반영 + notion_applied.json 이력) · 도입예정 등록 · 모드 잠금 ·
확장된 pending 키워드(볼린저 수축·재료 강도).
파일 수정은 모두 tmp 사본 + git_commit=False 라 실제 레포를 건드리지 않는다.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

from core.notion_client import extract_strategy_rules
from core.strategy import StrategyEditor

ROOT = Path(__file__).parents[2]
STRAT = ROOT / "config" / "strategy_params.yaml"

_spec = importlib.util.spec_from_file_location(
    "notion_apply", ROOT / "scripts" / "notion_apply.py",
)
na = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(na)


# ─────────────────────────── 매핑 / coercion ───────────────────────────

def test_agent_for_prefix() -> None:
    assert na._agent_for("screening.threshold")[0] == "screening"
    assert na._agent_for("signal.rsi.entry_zone")[0] == "signal"
    assert na._agent_for("stop_loss.hard_max_pct")[0] == "risk"
    assert na._agent_for("take_profit.step1.pct_range")[0] == "risk"
    assert na._agent_for("whatever.else")[0] == "ceo"


def test_coerce_value_forms() -> None:
    assert na.coerce_value("30") == 30
    assert na.coerce_value("0.02") == 0.02
    assert na.coerce_value("55~65") == [55, 65]
    assert na.coerce_value([2, 3]) == [2, 3]


# ─────────────────────────── pending 키워드 확장 ───────────────────────────

def test_pending_keywords_include_bollinger_and_material() -> None:
    knowledge = {"categories": {"signal": {"rules": [
        {"text": "볼린저밴드 수축(스퀴즈) 후 돌파에서 진입한다"},
        {"text": "재료(호재) 강도가 강한 종목만 본다"},
        {"text": "VWAP 위에서만 매수한다"},
    ]}}}
    _, pending = extract_strategy_rules(knowledge)
    labels = {p.label for p in pending}
    assert "볼린저밴드 수축(스퀴즈) 돌파" in labels
    assert "재료(뉴스·공시) 강도 필터" in labels
    assert "VWAP 기준선 진입 필터" in labels


# ─────────────────────────── extract ───────────────────────────

def _patch(monkeypatch, tmp_path: Path, knowledge: dict | None) -> Path:
    cfg = tmp_path / "strategy_params.yaml"
    shutil.copyfile(STRAT, cfg)
    monkeypatch.setattr(na, "CONFIG_PATH", cfg)
    kp = tmp_path / "notion_knowledge.json"
    if knowledge is not None:
        kp.write_text(json.dumps(knowledge, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(na, "KNOWLEDGE_PATH", kp)
    monkeypatch.setattr(na, "APPLIED_PATH", tmp_path / "notion_applied.json")
    monkeypatch.setattr(na, "MEMORY_DIR", tmp_path)
    return cfg


def test_extract_not_synced(monkeypatch, tmp_path: Path) -> None:
    _patch(monkeypatch, tmp_path, None)
    out = na.action_extract("paper")
    assert out["ok"] is True and out["synced"] is False and out["count"] == 0


def test_extract_builds_items_and_pending(monkeypatch, tmp_path: Path) -> None:
    import yaml
    cfg = _patch(monkeypatch, tmp_path, None)
    cur = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    cur_thr = int(cur["screening"]["threshold"])
    knowledge = {"title": "단타 커리큘럼", "categories": {
        "risk": {"rules": [{"text": "하드 손절 -4%로 한다"}]},
        "screening": {"rules": [{"text": f"스크리닝 통과 점수는 {cur_thr}점"}]},
        "signal": {"rules": [{"text": "VWAP 위에서만 진입한다"}]},
    }}
    kp = tmp_path / "notion_knowledge.json"
    kp.write_text(json.dumps(knowledge, ensure_ascii=False), encoding="utf-8")

    out = na.action_extract("paper")
    assert out["ok"] is True and out["synced"] is True
    by_key = {i["key"]: i for i in out["items"]}
    ts = by_key["stop_loss.hard_max_pct"]
    assert ts["applicable"] is True and abs(ts["proposed"] - (-0.04)) < 1e-9 and ts["agent"] == "risk"
    # 동일값은 적용 불가.
    same = by_key.get("screening.threshold")
    assert same is not None and same["applicable"] is False and "이미" in same["note"]
    # VWAP → 도입 가능(pending).
    assert any("VWAP" in p["label"] for p in out["pending"])


def test_extract_live_locks(monkeypatch, tmp_path: Path) -> None:
    _patch(monkeypatch, tmp_path, {"categories": {
        "risk": {"rules": [{"text": "하드 손절 -4%로 한다"}]}}})
    out = na.action_extract("live")
    it = next(i for i in out["items"] if i["key"] == "stop_loss.hard_max_pct")
    assert it["applicable"] is False and it["locked"] is True and "실전" in it["note"]


# ─────────────────────────── apply ───────────────────────────

def _editor(monkeypatch, tmp_path: Path) -> StrategyEditor:
    cfg = _patch(monkeypatch, tmp_path, {"categories": {}})
    return StrategyEditor(config_path=cfg, memory_dir=tmp_path, project_root=tmp_path,
                          mode="paper", git_commit=False)


def test_apply_writes_yaml_and_history(monkeypatch, tmp_path: Path) -> None:
    import yaml
    editor = _editor(monkeypatch, tmp_path)
    payload = {"items": [
        {"key": "stop_loss.hard_max_pct", "value": "-0.04",
         "label": "하드 손절(%)", "reason": "노션 손절 여유"}],
        "pending": [{"label": "손익비(R/R) 게이트", "sample": "R/R 2:1"}]}
    out = na.action_apply(payload, editor, "paper", ts="2026-06-02T10:00:00+09:00",
                          date="20260602")
    assert out["ok"] is True and len(out["applied"]) == 1
    assert abs(out["applied"][0]["to"] - (-0.04)) < 1e-9 and out["applied"][0]["agent"] == "risk"
    assert len(out["pending_registered"]) == 1
    # yaml 실제 반영.
    doc = yaml.safe_load(na.CONFIG_PATH.read_text(encoding="utf-8"))
    assert doc["stop_loss"]["hard_max_pct"] == -0.04
    # 이력 누적(param 1 + pending 1).
    hist = json.loads(na.APPLIED_PATH.read_text(encoding="utf-8"))
    assert len([h for h in hist if h["kind"] == "param"]) == 1
    assert len([h for h in hist if h["kind"] == "pending"]) == 1


def test_apply_blocked_in_live(monkeypatch, tmp_path: Path) -> None:
    editor = _editor(monkeypatch, tmp_path)
    out = na.action_apply({"items": [{"key": "stop_loss.hard_max_pct", "value": "-0.04"}]},
                          editor, "live", ts="t", date="20260602")
    assert out["ok"] is False and out["locked"] is True


def test_history_reads_applied(monkeypatch, tmp_path: Path) -> None:
    _patch(monkeypatch, tmp_path, {"categories": {}})
    na.APPLIED_PATH.write_text(json.dumps([
        {"ts": "2026-06-02T10:00:00+09:00", "kind": "param", "key": "stop_loss.hard_max_pct",
         "label": "하드 손절(%)", "from": -0.02, "to": -0.04},
        {"ts": "2026-06-02T10:01:00+09:00", "kind": "pending", "label": "VWAP 기준선 진입 필터"},
    ], ensure_ascii=False), encoding="utf-8")
    out = na.action_history()
    assert out["ok"] is True and out["total"] == 2
    assert out["param_count"] == 1 and out["pending_count"] == 1
