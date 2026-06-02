"""Unit tests — 회의 내용 적용 (scripts/meeting_apply.py + MeetingDecisionLog, §24).

검증: 값 coercion · 회의 결정 로그(누적/타임라인/롤백표시) · 실행 항목 추출(적용가능/
하드리밋잠금/실전잠금/동일값) · 실제 적용(yaml 반영+기록) · 롤백(원복+표시) · 모드 잠금.
파일 수정은 모두 tmp 사본에서 수행하고 git_commit=False 라 실제 레포를 건드리지 않는다.
"""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

from core.memory import MeetingDecisionLog
from core.strategy import StrategyEditor

ROOT = Path(__file__).parents[2]
STRAT = ROOT / "config" / "strategy_params.yaml"

_spec = importlib.util.spec_from_file_location(
    "meeting_apply", ROOT / "scripts" / "meeting_apply.py",
)
ma = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(ma)


# ─────────────────────────── coercion ───────────────────────────

def test_coerce_value_forms() -> None:
    assert ma.coerce_value("30") == 30
    assert ma.coerce_value("0.02") == 0.02
    assert ma.coerce_value("[55, 65]") == [55, 65]
    assert ma.coerce_value("55~65") == [55, 65]
    assert ma.coerce_value("55-65") == [55, 65]
    assert ma.coerce_value("true") is True
    assert ma.coerce_value(25) == 25
    assert ma.coerce_value([3, 5]) == [3, 5]
    assert ma.coerce_value("nope") == "nope"


# ─────────────────────────── MeetingDecisionLog ───────────────────────────

def test_decision_log_record_and_timeline(tmp_path: Path) -> None:
    log = MeetingDecisionLog.load(tmp_path)
    a = log.record(ts="2026-06-02T10:00:00+09:00", date="20260602",
                   meeting_id="m1", meeting_q="손절 잦음", meeting_ts="2026-06-02T09:00:00+09:00",
                   key="stop_loss.hard_max_pct", label="하드 손절(%)",
                   from_value=-0.02, to_value=-0.03, reason="손절 여유", improvement_id="imp1")
    log.record(ts="2026-06-02T10:01:00+09:00", date="20260602",
               meeting_id="m1", meeting_q="손절 잦음", meeting_ts="2026-06-02T09:00:00+09:00",
               key="screening.threshold", label="스크리닝 임계",
               from_value=70, to_value=75, reason="후보 까다롭게")
    # 디스크에서 다시 로드해도 보존.
    reloaded = MeetingDecisionLog.load(tmp_path)
    assert len(reloaded.decisions) == 2
    tl = reloaded.timeline()
    assert tl[0]["ts"] >= tl[1]["ts"]          # 최신순
    assert tl[0]["from"] == 70 and tl[0]["to"] == 75
    # find + mark_rolled_back
    assert reloaded.find(a.id) is not None
    reloaded.mark_rolled_back(a.id)
    assert MeetingDecisionLog.load(tmp_path).find(a.id).rolled_back is True


# ─────────────────────────── extract ───────────────────────────

def _patch_config(monkeypatch, tmp_path: Path) -> Path:
    cfg = tmp_path / "strategy_params.yaml"
    shutil.copyfile(STRAT, cfg)
    monkeypatch.setattr(ma, "CONFIG_PATH", cfg)
    return cfg


def test_extract_marks_applicable_hardlimit_and_same(monkeypatch, tmp_path: Path) -> None:
    import yaml
    cfg = _patch_config(monkeypatch, tmp_path)
    cur = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    cur_thr = cur["screening"]["threshold"]

    record = {
        "q": "손절이 너무 잦아요",
        "ceo": "1차 익절 비중을 늘리고 동시보유는 5종목으로 늘리자.",
        "improvements": [
            {"key": "take_profit.step1.close_ratio", "val": "0.6", "reason": "빨리 차익"},
            {"key": "max_concurrent_positions", "val": "5", "reason": "기회 확대"},   # 하드리밋
            {"key": "screening.threshold", "val": str(int(cur_thr)), "reason": "유지"},  # 동일값
        ],
    }
    out = ma.action_extract(record, "paper")
    assert out["ok"] is True
    by_key = {i["key"]: i for i in out["items"]}

    ts = by_key["take_profit.step1.close_ratio"]
    assert ts["applicable"] is True and ts["proposed"] == 0.6
    assert "→" in ts["display"]

    hl = by_key["max_concurrent_positions"]
    assert hl["hard_limit"] is True and hl["applicable"] is False and "하드리밋" in hl["note"]

    same = by_key["screening.threshold"]
    assert same["applicable"] is False and "이미 같은 값" in same["note"]


def test_extract_locks_in_live_mode(monkeypatch, tmp_path: Path) -> None:
    _patch_config(monkeypatch, tmp_path)
    record = {"improvements": [
        {"key": "take_profit.step1.close_ratio", "val": "0.6", "reason": "x"}]}
    out = ma.action_extract(record, "live")
    it = out["items"][0]
    assert it["applicable"] is False and it["locked"] is True and "실전" in it["note"]


def test_extract_from_free_text_only(monkeypatch, tmp_path: Path) -> None:
    _patch_config(monkeypatch, tmp_path)
    record = {"q": "", "ceo": "RSI 기준을 55~65로 좁히고 하드 손절 -3%로 하자."}
    out = ma.action_extract(record, "paper")
    keys = {i["key"] for i in out["items"]}
    assert "signal.rsi.entry_zone" in keys
    assert "stop_loss.hard_max_pct" in keys
    assert all(i["origin"] in ("meeting", "text") for i in out["items"])


# ─────────────────────────── apply + rollback ───────────────────────────

def _editor(monkeypatch, tmp_path: Path) -> StrategyEditor:
    cfg = _patch_config(monkeypatch, tmp_path)
    mem = tmp_path / "memory"
    mem.mkdir()
    monkeypatch.setattr(ma, "MEMORY_DIR", mem)
    return StrategyEditor(config_path=cfg, memory_dir=mem, project_root=tmp_path,
                          mode="paper", git_commit=False)


def test_apply_writes_yaml_and_records_decision(monkeypatch, tmp_path: Path) -> None:
    import yaml
    editor = _editor(monkeypatch, tmp_path)
    payload = {"meeting_id": "m1", "meeting_q": "손절 잦음", "items": [
        {"key": "take_profit.step1.close_ratio", "value": "0.6", "label": "1차 익절 비율",
         "reason": "빨리 차익"}]}
    out = ma.action_apply(payload, editor, "paper", ts="2026-06-02T10:00:00+09:00",
                          date="20260602")
    assert out["ok"] is True and len(out["applied"]) == 1
    a = out["applied"][0]
    assert a["to"] == 0.6 and a["decision_id"]
    # 파일에 실제 반영됐는지 재읽기 검증.
    doc = yaml.safe_load(ma.CONFIG_PATH.read_text(encoding="utf-8"))
    assert doc["take_profit"]["step1"]["close_ratio"] == 0.6
    # 회의 결정이 누적됐는지.
    log = MeetingDecisionLog.load(ma.MEMORY_DIR)
    assert len(log.decisions) == 1 and log.decisions[0].to_value == 0.6


def test_apply_blocked_in_live(monkeypatch, tmp_path: Path) -> None:
    editor = _editor(monkeypatch, tmp_path)   # editor mode paper, but action gets live
    out = ma.action_apply({"items": [{"key": "screening.threshold", "value": "75"}]},
                          editor, "live", ts="t", date="20260602")
    assert out["ok"] is False and out["locked"] is True


def test_apply_rejects_hard_limit_key(monkeypatch, tmp_path: Path) -> None:
    editor = _editor(monkeypatch, tmp_path)
    out = ma.action_apply({"items": [{"key": "max_concurrent_positions", "value": "5"}]},
                          editor, "paper", ts="t", date="20260602")
    assert out["ok"] is False and len(out["failed"]) == 1


def test_rollback_restores_value(monkeypatch, tmp_path: Path) -> None:
    import yaml
    editor = _editor(monkeypatch, tmp_path)
    before = yaml.safe_load(ma.CONFIG_PATH.read_text(encoding="utf-8"))["take_profit"][
        "step1"]["close_ratio"]
    applied = ma.action_apply(
        {"meeting_id": "m1", "items": [
            {"key": "take_profit.step1.close_ratio", "value": "0.6", "label": "1차 익절 비율"}]},
        editor, "paper", ts="t1", date="20260602")
    did = applied["applied"][0]["decision_id"]
    # 롤백.
    out = ma.action_rollback({"id": did}, editor, "paper", ts="t2", date="20260602")
    assert out["ok"] is True
    doc = yaml.safe_load(ma.CONFIG_PATH.read_text(encoding="utf-8"))
    assert doc["take_profit"]["step1"]["close_ratio"] == before     # 원복
    log = MeetingDecisionLog.load(ma.MEMORY_DIR)
    orig = log.find(did)
    assert orig.rolled_back is True
    assert any(d.rollback_of == did for d in log.decisions)     # 롤백 기록 추가


def test_rollback_unknown_id(monkeypatch, tmp_path: Path) -> None:
    editor = _editor(monkeypatch, tmp_path)
    out = ma.action_rollback({"id": "zzz"}, editor, "paper", ts="t", date="20260602")
    assert out["ok"] is False and "찾을 수 없" in out["reason"]
