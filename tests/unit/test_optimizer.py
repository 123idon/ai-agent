"""Unit tests for the meta OptimizerAgent (CLAUDE.md §2.7, §11)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

from agents.ceo.main import CeoAgent
from agents.meta.optimizer.main import (
    AGENT_NAME,
    OptimizationProposal,
    OptimizerAgent,
    ParamChange,
    apply_proposal_to_file,
    set_yaml_scalar,
)
from core.kis_client import KisClient, KisClientConfig, Mode
from core.messaging import Bus

ROOT = Path(__file__).parents[2]
STRAT = ROOT / "config" / "strategy_params.yaml"
KST = timezone(timedelta(hours=9))


# ─────────────────────────── journal 헬퍼 ───────────────────────────


def _write_journal(path: Path, records: list[tuple[str, dict, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for topic, payload, ts in records:
            f.write(json.dumps(
                {"topic": topic, "ts": ts, "mode": "paper", "sender": "bus",
                 "payload": payload},
                ensure_ascii=False,
            ) + "\n")


def _exit(pnl: float) -> dict:
    return {"symbol": "005930", "kind": "take_profit_1", "pnl_pct": pnl, "qty": 1, "price": 1}


def _optimizer(journal_dir: Path, *, mode: Mode = Mode.PAPER) -> tuple[OptimizerAgent, Bus]:
    bus = Bus()
    opt = OptimizerAgent(mode, bus, journal_dir, config_path=STRAT)
    return opt, bus


# ─────────────────────────── 1) 성과 관찰 ───────────────────────────


async def test_performance_metrics(tmp_path: Path) -> None:
    date = "20260529"
    _write_journal(tmp_path / f"{date}.jsonl", [
        ("signal.exit", _exit(0.04), "t1"),
        ("signal.exit", _exit(-0.02), "t2"),
        ("signal.exit", _exit(0.03), "t3"),
        ("signal.exit", _exit(-0.03), "t4"),
        ("signal.exit", _exit(0.05), "t5"),
    ])
    opt, _ = _optimizer(tmp_path)
    report = await opt.observe(date)
    perf = report.performance
    assert perf.trades == 5
    assert perf.wins == 3 and perf.losses == 2
    assert abs(perf.win_rate - 0.6) < 1e-9
    assert abs(perf.profit_factor - (0.12 / 0.05)) < 1e-9
    assert perf.payoff_ratio is not None
    assert abs(perf.signal_accuracy - 0.6) < 1e-9


# ─────────────────────────── 2) 조건 분석 ───────────────────────────


async def test_condition_breakdown(tmp_path: Path) -> None:
    date = "20260529"
    _write_journal(tmp_path / f"{date}.jsonl", [
        ("screening.candidates", {"code": "AAA", "themes": ["AI"], "score": 80}, "t1"),
        ("order.event", {"symbol": "AAA", "side": "buy",
                         "approved": {"entry_signal": {"signal": "STRONG_ENTRY"}}}, "t2"),
        ("signal.exit", {"symbol": "AAA", "kind": "take_profit_1", "pnl_pct": 0.04}, "t3"),
        ("order.event", {"symbol": "BBB", "side": "buy",
                         "approved": {"entry_signal": {"signal": "CONDITIONAL_ENTRY"}}}, "t4"),
        ("signal.exit", {"symbol": "BBB", "kind": "hard_stop_loss", "pnl_pct": -0.03}, "t5"),
    ])
    opt, _ = _optimizer(tmp_path)
    report = await opt.observe(date)
    by_entry = {s.key: s for s in report.conditions.by_entry_signal}
    assert by_entry["STRONG_ENTRY"].wins == 1
    assert by_entry["CONDITIONAL_ENTRY"].wins == 0
    by_exit = {s.key: s for s in report.conditions.by_exit_kind}
    assert "take_profit_1" in by_exit and "hard_stop_loss" in by_exit
    by_theme = {s.key: s for s in report.conditions.by_theme}
    assert by_theme["AI"].trades == 1


# ─────────────────────────── 4) 토큰 최적화 ───────────────────────────


async def test_token_waste_detection(tmp_path: Path) -> None:
    date = "20260529"
    records = [
        ("meta.claude_call", {"agent": "analysis.signal", "route": "/api/claude",
                              "purpose": "decision", "body": {"q": 1}}, "t1"),
        ("meta.claude_call", {"agent": "learning.pattern", "route": "/api/claude",
                              "purpose": "summary", "body": {"q": 9}}, "t2"),
        ("meta.claude_call", {"agent": "learning.pattern", "route": "/api/claude",
                              "purpose": "summary", "body": {"q": 9}}, "t3"),
    ]
    # 과다 호출 에이전트 (60 > 임계 50)
    records += [
        ("meta.claude_call", {"agent": "intel.screening", "route": "/api/claude",
                              "purpose": "summary", "body": {"i": i}}, f"h{i}")
        for i in range(60)
    ]
    _write_journal(tmp_path / f"{date}.jsonl", records)
    opt, _ = _optimizer(tmp_path)
    report = await opt.observe(date)
    blob = " | ".join(report.tokens.waste_findings)
    assert "CRITICAL" in blob          # 매매 결정 위임 (§15.4)
    assert "REDUNDANT" in blob         # 동일 호출 2회
    assert "HIGH_VOLUME" in blob       # 과다 호출
    assert report.tokens.total_calls == 63


# ─────────────────────────── 3) 제안 모드 게이트 ───────────────────────────


async def test_propose_paper_generates_strategy(tmp_path: Path) -> None:
    date = "20260529"
    # profit_factor < 1.0, trades >= 5 → screening.threshold 상향 제안
    _write_journal(tmp_path / f"{date}.jsonl", [
        ("signal.exit", _exit(0.01), "t1"),
        ("signal.exit", _exit(0.01), "t2"),
        ("signal.exit", _exit(-0.03), "t3"),
        ("signal.exit", _exit(-0.03), "t4"),
        ("signal.exit", _exit(-0.03), "t5"),
    ])
    opt, bus = _optimizer(tmp_path, mode=Mode.PAPER)
    published = bus.collector("learning.proposal")
    report, proposals = await opt.run_once(date)
    strat = [p for p in proposals if p.kind == "strategy"]
    assert strat, "paper에서 전략 제안이 생성되어야 함"
    change = strat[0].changes[0]
    assert change.key == "screening.threshold"
    # 현재 임계값 + 5 (저성과 → 선별 강화). repo 설정값에 종속되지 않게 동적 검증.
    base_threshold = yaml.safe_load(STRAT.read_text(encoding="utf-8"))["screening"]["threshold"]
    assert change.to_value == base_threshold + 5
    assert strat[0].auto_apply is False
    assert len(published) == len(proposals)


async def test_propose_live_is_observe_only(tmp_path: Path) -> None:
    date = "20260529"
    _write_journal(tmp_path / f"{date}.jsonl", [
        ("signal.exit", _exit(-0.03), f"t{i}") for i in range(6)
    ])
    opt, _ = _optimizer(tmp_path, mode=Mode.LIVE)
    report, proposals = await opt.run_once(date)
    assert proposals == []               # live: 관찰/수집만 (§11)


# ─────────────────────────── 5) 자기 관찰 ───────────────────────────


async def test_self_observation(tmp_path: Path) -> None:
    date = "20260529"
    _write_journal(tmp_path / f"{date}.jsonl", [("signal.exit", _exit(0.04), "t1")])
    opt, _ = _optimizer(tmp_path)
    r1 = await opt.observe(date)
    r2 = await opt.observe(date)
    assert AGENT_NAME in r1.observed_agents
    assert r1.self_stats["observe_runs"] == 1
    assert r2.self_stats["observe_runs"] == 2


# ─────────────────────────── YAML 적용기 ───────────────────────────


def test_set_yaml_scalar_preserves_comments() -> None:
    text = STRAT.read_text(encoding="utf-8")
    new_text, ok = set_yaml_scalar(text, "screening.threshold", 75)
    assert ok
    assert "# 전일 거래대금" in new_text         # 주석 보존
    assert yaml.safe_load(new_text)["screening"]["threshold"] == 75


def test_set_yaml_scalar_nested_key() -> None:
    text = STRAT.read_text(encoding="utf-8")
    new_text, ok = set_yaml_scalar(text, "stop_loss.hard_max_pct", -0.04)
    assert ok
    assert yaml.safe_load(new_text)["stop_loss"]["hard_max_pct"] == -0.04


def test_set_yaml_scalar_refuses_mapping_and_list() -> None:
    text = STRAT.read_text(encoding="utf-8")
    _, ok_map = set_yaml_scalar(text, "screening", 5)        # 매핑
    _, ok_list = set_yaml_scalar(text, "signal.ma_periods", 5)  # 리스트
    assert ok_map is False
    assert ok_list is False


def test_apply_refuses_hard_limits(tmp_path: Path) -> None:
    hl = tmp_path / "hard_limits.yaml"
    hl.write_text("max_concurrent_positions: 3\n", encoding="utf-8")
    prop = OptimizationProposal(
        proposal_id="x", kind="strategy", mode="paper", created_ts="t",
        rationale="r",
        changes=(ParamChange("max_concurrent_positions", 3, 5, "r"),),
    )
    try:
        apply_proposal_to_file(prop, hl)
        raise AssertionError("하드리밋 적용은 ValueError여야 함")
    except ValueError:
        pass


def test_apply_whitelist_only(tmp_path: Path) -> None:
    cfg = tmp_path / "strategy_params.yaml"
    cfg.write_text(STRAT.read_text(encoding="utf-8"), encoding="utf-8")
    # version은 화이트리스트 밖 → 적용 거부
    prop = OptimizationProposal(
        proposal_id="x", kind="strategy", mode="paper", created_ts="t", rationale="r",
        changes=(ParamChange("version", "0.2.0", "9.9.9", "r"),),
    )
    applied = apply_proposal_to_file(prop, cfg)
    assert applied == []
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["version"] == "0.2.0"


# ─────────────────────────── CEO 승인 흐름 ───────────────────────────


def _ceo(tmp_root: Path, mode: Mode) -> tuple[CeoAgent, Bus]:
    (tmp_root / "config").mkdir(parents=True, exist_ok=True)
    (tmp_root / "config" / "strategy_params.yaml").write_text(
        STRAT.read_text(encoding="utf-8"), encoding="utf-8",
    )
    cfg = KisClientConfig(
        base_url="http://t.test", app_key="AK", app_secret="AS",
        account="1-01", mode=mode,
    )
    http = httpx.AsyncClient(
        base_url="http://t.test",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})),
        timeout=httpx.Timeout(6.0),
    )
    bus = Bus()
    return CeoAgent(KisClient(cfg, http_client=http), bus, project_root=tmp_root), bus


def _proposal() -> OptimizationProposal:
    return OptimizationProposal(
        proposal_id="p1", kind="strategy", mode="paper", created_ts="t",
        rationale="r",
        changes=(ParamChange("screening.threshold", 70, 75, "더 선별적"),),
    )


async def test_ceo_pending_and_approve_paper(tmp_path: Path) -> None:
    ceo, bus = _ceo(tmp_path, Mode.PAPER)
    await bus.publish("learning.proposal", _proposal())
    assert "p1" in ceo.pending_proposals
    ok = ceo.approve_proposal("p1")
    assert ok is True
    cfg = tmp_path / "config" / "strategy_params.yaml"
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["screening"]["threshold"] == 75
    assert "p1" not in ceo.pending_proposals       # 처리 후 큐에서 제거


async def test_ceo_live_refuses_apply(tmp_path: Path) -> None:
    ceo, bus = _ceo(tmp_path, Mode.LIVE)
    cfg = tmp_path / "config" / "strategy_params.yaml"
    before = yaml.safe_load(cfg.read_text(encoding="utf-8"))["screening"]["threshold"]
    await bus.publish("learning.proposal", _proposal())
    ok = ceo.approve_proposal("p1")
    assert ok is False                              # live: 파라미터 잠금(§3.3)
    # 거부됐으므로 임계값은 그대로(변경 없음).
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["screening"]["threshold"] == before


async def test_ceo_token_proposal_approved_as_recommendation(tmp_path: Path) -> None:
    ceo, bus = _ceo(tmp_path, Mode.PAPER)
    token_prop = OptimizationProposal(
        proposal_id="t1", kind="token", mode="paper", created_ts="t", rationale="r",
        recommendations=("줄이세요",),
    )
    await bus.publish("learning.proposal", token_prop)
    assert ceo.approve_proposal("t1") is True       # 변경 없음 → 권고 승인 기록
    assert "t1" not in ceo.pending_proposals


async def test_ceo_reject_and_ignores_non_proposal(tmp_path: Path) -> None:
    ceo, bus = _ceo(tmp_path, Mode.PAPER)
    await bus.publish("learning.proposal", _proposal())
    assert ceo.reject_proposal("p1") is True
    assert ceo.reject_proposal("nope") is False
    # proposal_id 없는 페이로드(DailySummary 등)는 무시
    await bus.publish("learning.proposal", {"date": "20260529", "record_count": 0})
    assert ceo.pending_proposals == {}
