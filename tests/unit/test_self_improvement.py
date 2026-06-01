"""상담/복기/노션 → 전략 반영 + 영구 기억 파이프라인 단위 테스트.

CLAUDE.md §2.7/§11/§13/§19/§23 — 상담·복기·노션이 실제 파라미터를 바꾸고,
그 변경이 영구 기억으로 누적되어 다음 세션에 반영되는지 검증한다.
"""
from __future__ import annotations

import json
from pathlib import Path

from agents.learning.notion_sync import NotionSyncAgent
from agents.meta.optimizer.main import set_yaml_leaf
from core.consult import extract_changes
from core.learning import ReviewLearner
from core.memory import ConsultLog, ImprovementLog, session_learning_brief
from core.notion_client import NotionConfig, extract_strategy_rules
from core.strategy import StrategyEditor

_YAML = """\
screening:
  threshold: 85          # 코멘트 보존 확인
signal:
  volume_surge_multiplier: 2.5
  rsi:
    entry_zone: [50, 65]
    overbought: 70
stop_loss:
  technical_stop_enabled: true
  technical_buffer_pct: 0.005
  hard_max_pct: -0.03
take_profit:
  step1:
    pct_range: [0.03, 0.05]
    close_ratio: 0.5
time_stop:
  enabled: true
  evaluation_minutes: 30
  min_profit_pct: 0.01
  action: "reduce_50"
  first_check_minutes: 15
  first_check_action: "hold"
"""


def _config(tmp_path: Path) -> Path:
    p = tmp_path / "strategy_params.yaml"
    p.write_text(_YAML, encoding="utf-8")
    return p


# ─────────────────────────── set_yaml_leaf ───────────────────────────


def test_set_yaml_leaf_scalar_preserves_comment() -> None:
    new, ok = set_yaml_leaf(_YAML, "screening.threshold", 88)
    assert ok
    assert "threshold: 88" in new
    assert "코멘트 보존 확인" in new   # 주석 보존


def test_set_yaml_leaf_inline_list() -> None:
    new, ok = set_yaml_leaf(_YAML, "signal.rsi.entry_zone", [55, 65])
    assert ok
    assert "entry_zone: [55, 65]" in new


def test_set_yaml_leaf_refuses_mapping() -> None:
    _, ok = set_yaml_leaf(_YAML, "signal", 5)
    assert not ok


# ─────────────────────────── consult parser ───────────────────────────


def test_parser_rsi_range() -> None:
    sugg = {s.key: s.value for s in extract_changes("RSI 기준을 55~65로 좁히자")}
    assert sugg["signal.rsi.entry_zone"] == [55, 65]


def test_parser_timestop_and_hardstop() -> None:
    sugg = {s.key: s.value for s in extract_changes("타임스톱 25분으로 하고 하드 손절 -2%로")}
    assert sugg["time_stop.evaluation_minutes"] == 25
    assert abs(sugg["stop_loss.hard_max_pct"] - (-0.02)) < 1e-9


def test_parser_ignores_vague_text() -> None:
    assert extract_changes("오늘 시장이 안 좋네요 조심합시다") == []


def test_parser_technical_buffer_not_confused_with_hard_stop() -> None:
    # "기술적 손절 버퍼 1%" → technical_buffer_pct (하드 손절로 오인하지 않음)
    sugg = {s.key: s.value for s in extract_changes("기술적 손절 버퍼를 1%로 늘리자")}
    assert abs(sugg["stop_loss.technical_buffer_pct"] - 0.01) < 1e-9
    assert "stop_loss.hard_max_pct" not in sugg


def test_parser_first_check_and_main_timestop() -> None:
    sugg = {s.key: s.value for s in
            extract_changes("1차 타임스톱 10분, 타임스톱 25분으로 하고 전량 청산으로")}
    assert sugg["time_stop.first_check_minutes"] == 10
    assert sugg["time_stop.evaluation_minutes"] == 25
    assert sugg["time_stop.action"] == "exit_all"


def test_parser_timestop_min_profit_and_toggle() -> None:
    sugg = {s.key: s.value for s in
            extract_changes("타임스톱 수익 0.5% 미만이면 정리, 기술적 손절 끄기")}
    assert abs(sugg["time_stop.min_profit_pct"] - 0.005) < 1e-9
    assert sugg["stop_loss.technical_stop_enabled"] is False


# ─────────────────────────── StrategyEditor ───────────────────────────


def test_editor_applies_scalar_and_records(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    editor = StrategyEditor(
        config_path=cfg, memory_dir=tmp_path, project_root=tmp_path,
        mode="paper", git_commit=False,
    )
    res = editor.apply(
        "screening.threshold", 78, ts="2026-06-01T10:00:00+09:00",
        date="20260601", source="consult", label="스크리닝 임계",
    )
    assert res.ok
    assert res.before == 85 and res.after == 78
    assert "스크리닝 임계: 85 → 78로 변경됨" == res.display
    # 영구 기록 확인
    imp = ImprovementLog.load(tmp_path)
    assert len(imp.entries) == 1
    assert imp.entries[0].key == "screening.threshold"


def test_editor_applies_list(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    editor = StrategyEditor(
        config_path=cfg, memory_dir=tmp_path, project_root=tmp_path,
        mode="paper", git_commit=False,
    )
    res = editor.apply(
        "signal.rsi.entry_zone", [55, 65], ts="t", date="20260601",
        source="consult", label="RSI 진입 구간",
    )
    assert res.ok
    assert res.after == [55, 65]
    assert "[50, 65] → [55, 65]" in res.display


def test_editor_rejects_non_whitelist(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    editor = StrategyEditor(config_path=cfg, memory_dir=tmp_path,
                            project_root=tmp_path, mode="paper", git_commit=False)
    res = editor.apply("hard_limits.max_positions", 5, ts="t", date="d", source="consult")
    assert not res.ok
    assert "보호된 키" in res.reason


def test_editor_locked_in_live(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    editor = StrategyEditor(config_path=cfg, memory_dir=tmp_path,
                            project_root=tmp_path, mode="live", git_commit=False)
    res = editor.apply("screening.threshold", 88, ts="t", date="d", source="consult")
    assert not res.ok and res.locked


# ───────────── funnel/진입 안전범위 클램프 (§19 죽음의 나선 방지) ─────────────


def _paper_editor(tmp_path: Path) -> StrategyEditor:
    return StrategyEditor(config_path=_config(tmp_path), memory_dir=tmp_path,
                          project_root=tmp_path, mode="paper", git_commit=False)


def test_clamp_threshold_over_max_is_bounded(tmp_path: Path) -> None:
    # consult/auto-learn 가 threshold 를 90 으로 끌어올리려 해도 funnel 굶음 방지 상한 80 으로 보정.
    editor = _paper_editor(tmp_path)
    res = editor.apply("screening.threshold", 90, ts="t", date="d", source="review")
    assert res.ok
    assert res.after == 80                      # 90 → 80 보정
    assert "보정" in res.reason and "§19" in res.reason


def test_clamp_threshold_within_range_unchanged(tmp_path: Path) -> None:
    editor = _paper_editor(tmp_path)
    res = editor.apply("screening.threshold", 75, ts="t", date="d", source="consult")
    assert res.ok and res.after == 75           # 범위 안 → 그대로
    assert "보정" not in res.reason


def test_clamp_rejects_when_clamped_equals_current(tmp_path: Path) -> None:
    # 이미 상한(80)에 있는 상태에서 더 조이려는 요청은 보정 후 현재값과 같아 변경 없음(과강화 차단).
    editor = _paper_editor(tmp_path)
    editor.apply("screening.threshold", 80, ts="t", date="d", source="review")
    res = editor.apply("screening.threshold", 95, ts="t2", date="d", source="review")
    assert not res.ok and "변경할 게 없" in res.reason


def test_clamp_entry_zone_low_bounded(tmp_path: Path) -> None:
    # RSI 진입창의 하단을 60 으로 올려 진입을 굶기려 해도 low 상한 55 로 보정.
    editor = _paper_editor(tmp_path)
    res = editor.apply("signal.rsi.entry_zone", [60, 65], ts="t", date="d", source="review")
    assert res.ok
    assert res.after == [55, 65]                 # low 60 → 55 보정
    assert "보정" in res.reason


def test_hard_stop_within_range_applied_as_is(tmp_path: Path) -> None:
    # 손절 안전범위(-0.5%~-10%, 요구 5) 안의 값은 보정 없이 그대로 적용.
    editor = _paper_editor(tmp_path)
    res = editor.apply("stop_loss.hard_max_pct", -0.01, ts="t", date="d", source="review")
    assert res.ok
    assert abs(res.after - (-0.01)) < 1e-9        # -1%는 범위 안 → 그대로
    assert "보정" not in res.reason


def test_hard_stop_too_tight_clamped_to_min(tmp_path: Path) -> None:
    # -0.3%(너무 빡빡, 잦은 손절)는 하한 -0.5%로 보정.
    editor = _paper_editor(tmp_path)
    res = editor.apply("stop_loss.hard_max_pct", -0.003, ts="t", date="d", source="review")
    assert res.ok
    assert abs(res.after - (-0.005)) < 1e-9
    assert "보정" in res.reason


def test_hard_stop_too_loose_clamped_to_max(tmp_path: Path) -> None:
    # -15%(너무 느슨, 큰 손실)는 상한 -10%로 보정(요구 5 안전장치).
    editor = _paper_editor(tmp_path)
    res = editor.apply("stop_loss.hard_max_pct", -0.15, ts="t", date="d", source="review")
    assert res.ok
    assert abs(res.after - (-0.10)) < 1e-9
    assert "보정" in res.reason


def test_time_stop_min_profit_clamped(tmp_path: Path) -> None:
    # 타임스톱 수익 기준 안전범위 0~5%: 10% 요청 → 5%로 보정.
    editor = _paper_editor(tmp_path)
    res = editor.apply("time_stop.min_profit_pct", 0.10, ts="t", date="d", source="review")
    assert res.ok
    assert abs(res.after - 0.05) < 1e-9
    assert "보정" in res.reason


# ─────────────────────────── ConsultLog ───────────────────────────


def test_consult_log_persists_and_loads_context(tmp_path: Path) -> None:
    log = ConsultLog.load(tmp_path)
    log.add_turn(ts="t1", role="operator", text="RSI 좁히자",
                 applied=[{"key": "signal.rsi.entry_zone", "from": [50, 65], "to": [55, 65]}])
    # 새로 로드해도 누적 유지(세션 간 기억)
    reloaded = ConsultLog.load(tmp_path)
    assert len(reloaded.turns) == 1
    assert "RSI" in reloaded.context_brief()
    last = reloaded.last_change_for_key("signal.rsi.entry_zone")
    assert last is not None and last["to"] == [55, 65]


# ─────────────────────────── ReviewLearner ───────────────────────────


def _exit(ts: str, kind: str, pnl: float) -> dict:
    return {"topic": "signal.exit", "ts": ts, "payload": {"kind": kind, "pnl_pct": pnl}}


def test_review_consecutive_stoploss_narrows_rsi() -> None:
    records = [
        _exit("t1", "technical_stop", -0.01),
        _exit("t2", "hard_stop_loss", -0.02),
        _exit("t3", "technical_stop", -0.01),
        _exit("t4", "take_profit_1", 0.03),
        _exit("t5", "time_stop", 0.0),
    ]
    learner = ReviewLearner(lambda k: {"signal.rsi.entry_zone": [50, 65],
                                       "time_stop.evaluation_minutes": 30}.get(k))
    sugg = {s.key: s for s in learner.analyze(records)}
    assert "signal.rsi.entry_zone" in sugg
    assert sugg["signal.rsi.entry_zone"].to_value == [55, 65]


def test_review_timestop_heavy_shortens() -> None:
    records = [_exit(f"t{i}", "time_stop", 0.0) for i in range(4)] + [
        _exit("t9", "take_profit_1", 0.03)
    ]
    learner = ReviewLearner(lambda k: {"signal.rsi.entry_zone": [50, 65],
                                       "time_stop.evaluation_minutes": 30}.get(k))
    sugg = {s.key: s for s in learner.analyze(records)}
    assert sugg["time_stop.evaluation_minutes"].to_value == 25


# ─────────────────────────── ImprovementLog 효과/롤백 ───────────────────────────


def test_improvement_evaluate_effects_and_rollback(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    journal.mkdir()
    # 변경 전(20260530): 평균 +1% / 변경 후(20260601~): 평균 -1%  → worse
    (journal / "20260530.jsonl").write_text(
        json.dumps(_exit("a", "take_profit_1", 0.01)) + "\n", encoding="utf-8")
    (journal / "20260601.jsonl").write_text(
        json.dumps(_exit("b", "hard_stop_loss", -0.01)) + "\n", encoding="utf-8")
    imp = ImprovementLog.load(tmp_path)
    imp.record(ts="2026-06-01T09:00:00+09:00", date="20260601", source="consult",
               key="screening.threshold", from_value=85, to_value=80)
    imp.evaluate_effects(journal)
    rb = imp.rollback_candidates()
    assert rb and rb[0]["key"] == "screening.threshold"
    assert rb[0]["rollback_to"] == 85


# ─────────────────────────── session brief ───────────────────────────


def test_session_brief_remembers(tmp_path: Path) -> None:
    imp = ImprovementLog.load(tmp_path)
    imp.record(ts="t", date="20260601", source="consult",
               key="time_stop.evaluation_minutes", from_value=30, to_value=25)
    brief = session_learning_brief(tmp_path)
    assert "time_stop.evaluation_minutes 30→25" in brief


# ─────────────────────────── Notion → strategy ───────────────────────────


def test_notion_extract_strategy_rules() -> None:
    knowledge = {"categories": {
        "signal": {"rules": [{"text": "RSI 55~65 구간에서만 진입한다"}]},
        "risk": {"rules": [{"text": "VWAP 아래에서는 진입 금지"},
                           {"text": "타임스톱 20분으로 한다"}]},
    }}
    sugg, pending = extract_strategy_rules(knowledge)
    keys = {s.key for s in sugg}
    assert "signal.rsi.entry_zone" in keys
    assert "time_stop.evaluation_minutes" in keys
    assert any("VWAP" in p.label for p in pending)


def test_notion_apply_to_strategy(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    memdir = tmp_path / "data" / "memory"
    memdir.mkdir(parents=True)
    # 노션 지식 파일 작성
    knowledge = {
        "title": "단타 커리큘럼", "source_page_id": "pid",
        "fetched_at": "t", "content_hash": "h",
        "categories": {
            "signal": {"rules": [{"text": "RSI 55~65 구간 진입"}]},
            "risk": {"rules": [{"text": "VWAP 필터 도입"}]},
        },
        "stats": {"total_rules": 2},
    }
    (memdir / "notion_knowledge.json").write_text(
        json.dumps(knowledge, ensure_ascii=False), encoding="utf-8")

    agent = NotionSyncAgent(NotionConfig(token="-", page_id="pid"), memory_dir=memdir)
    editor = StrategyEditor(config_path=cfg, memory_dir=memdir,
                            project_root=tmp_path, mode="paper", git_commit=False)
    out = agent.apply_to_strategy(editor, ts="t", date="20260601")
    assert out["ok"]
    assert any(a["key"] == "signal.rsi.entry_zone" for a in out["applied"])
    assert any("VWAP" in p["label"] for p in out["pending"])
    # status 에 반영 현황 노출
    st = agent.status()
    assert st["applied_rules"]
    assert st["pending_rules"]
