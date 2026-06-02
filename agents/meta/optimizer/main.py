"""메타 최적화 에이전트 (CLAUDE.md §2.7, §11, §13).

전 에이전트의 성과·토큰 사용을 관찰하고, **모의(paper) 모드에서만** 전략 진화/
토큰 최적화 제안을 생성한다. 제안은 절대 자동 적용되지 않으며, 반드시 CEO 에이전트
승인(``CeoAgent.approve_proposal``)을 거쳐야 ``config/strategy_params.yaml``에 반영된다.

작동 조건:
  - live  : 관찰/수집만 (``propose``는 빈 리스트 반환)
  - paper : 진화/최적화 제안 가능
  - 적용  : CEO 승인 후에만 (본 에이전트는 파일을 직접 쓰지 않는다)

자기 자신(``meta.optimizer``)도 관찰 대상에 포함한다(§2.7).
하드리밋(``config/hard_limits.yaml``, §4)은 제안 대상이 될 수 없다(§13.1).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.kis_client import Mode
from core.messaging import Bus

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

TOPIC_OBSERVATION = "meta.observation"
TOPIC_PROPOSAL = "learning.proposal"   # CEO가 구독하는 기존 제안 채널 (§6.1)
TOPIC_CLAUDE_CALL = "meta.claude_call"

AGENT_NAME = "meta.optimizer"

# 관찰 대상 에이전트 (자기 자신 포함, §2.7-5)
KNOWN_AGENTS = (
    "ceo",
    "intel.screening",
    "intel.market_watch",
    "analysis.signal",
    "risk.risk_manager",
    "execution.order",
    "execution.position_manager",
    "learning.journal",
    "learning.pattern",
    "learning.postmortem",
    AGENT_NAME,
)

# strategy_params.yaml에서 제안/상담/노션/복기가 건드릴 수 있는 안전한 키 화이트리스트.
# 하드리밋(config/hard_limits.yaml, §4)·구조(매핑) 값은 절대 제외한다(§4, §13).
# 스칼라뿐 아니라 **인라인 리스트 leaf**(RSI 구간·익절 범위)도 허용한다(set_yaml_leaf).
TUNABLE_KEYS = frozenset({
    "screening.threshold",
    "signal.volume_surge_multiplier",
    "signal.rsi.entry_zone",                       # [low, high] 인라인 리스트
    "signal.rsi.overbought",
    # 신호 진입 조건 충족 개수 (§5.2) — 상담에서 "신호 조건 N개로" 변경 가능.
    "signal.entry_rules.strong_min_indicators",    # STRONG 판정 기준(=진입 필요 개수)
    "signal.entry_rules.conditional_min_indicators",  # CONDITIONAL 판정 기준
    # 5분봉 돌파 타점 (§5.2) — 실제 매매에 쓰이는 거래량 배수/룩백.
    "signal.breakout.volume_mult",
    "signal.breakout.lookback",
    # 포지션 사이징 (§25.3) — 진입 비중/신용 배수.
    "entry.sizing.cash_fraction_strong",
    "entry.sizing.cash_fraction_conditional",
    "entry.sizing.credit_multiplier",
    "entry.conditional_cap_pct",
    # 손절 (§5.4) — 상담/회의에서 조정 가능
    "stop_loss.hard_max_pct",
    "stop_loss.technical_stop_enabled",
    "stop_loss.technical_buffer_pct",
    "stop_loss.signal_breakdown_grace_minutes",
    "take_profit.step1.pct_range",                 # 인라인 리스트
    "take_profit.step1.close_ratio",
    "take_profit.step2.pct_range",                 # 인라인 리스트
    "take_profit.step2.close_ratio",
    "take_profit.step3_trailing.trail_from_high_pct",
    # 타임스톱(시간 기반 매도)은 제거되었다(§5.5) — 조정 가능한 time_stop 키 없음.
})

# 인라인 리스트(leaf) 값을 허용하는 키 — set_yaml_leaf가 [a, b] 형태로 교체한다.
LIST_KEYS = frozenset({
    "signal.rsi.entry_zone",
    "take_profit.step1.pct_range",
    "take_profit.step2.pct_range",
})


# ─────────────────────────── 리포트 모델 ───────────────────────────


@dataclass(frozen=True)
class PerformanceReport:
    trades: int
    wins: int
    losses: int
    win_rate: float
    gross_profit_pct: float
    gross_loss_pct: float
    profit_factor: float | None      # 손익비(이익합/손실합), 손실 0이면 None
    avg_win_pct: float
    avg_loss_pct: float
    payoff_ratio: float | None       # 평균이익/평균손실
    signal_accuracy: float           # 진입 신호가 비손실로 이어진 비율


@dataclass(frozen=True)
class ConditionStat:
    key: str
    trades: int
    wins: int
    win_rate: float
    avg_pnl_pct: float


@dataclass(frozen=True)
class ConditionReport:
    by_entry_signal: tuple[ConditionStat, ...]
    by_exit_kind: tuple[ConditionStat, ...]
    by_theme: tuple[ConditionStat, ...]


@dataclass(frozen=True)
class AgentTokenStat:
    agent: str
    calls: int
    by_route: dict[str, int]
    by_purpose: dict[str, int]


@dataclass(frozen=True)
class TokenUsageReport:
    total_calls: int
    by_agent: tuple[AgentTokenStat, ...]
    waste_findings: tuple[str, ...]   # 불필요/위반 호출 탐지 결과


@dataclass(frozen=True)
class ObservationReport:
    date: str
    mode: str
    performance: PerformanceReport
    conditions: ConditionReport
    tokens: TokenUsageReport
    observed_agents: tuple[str, ...]
    self_stats: dict[str, Any]


# ─────────────────────────── 제안 모델 ───────────────────────────


@dataclass(frozen=True)
class ParamChange:
    key: str            # strategy_params.yaml 내 점(.) 경로 (TUNABLE_KEYS만 허용)
    from_value: Any
    to_value: Any
    reason: str


@dataclass(frozen=True)
class OptimizationProposal:
    proposal_id: str
    kind: str           # "strategy" | "token"
    mode: str
    created_ts: str
    rationale: str
    changes: tuple[ParamChange, ...] = ()
    recommendations: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)
    requires_approval: bool = True
    auto_apply: bool = False   # 절대 True 금지 (§11)


# ─────────────────────────── journal 읽기 ───────────────────────────


def _read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _payload(rec: dict[str, Any]) -> dict[str, Any]:
    p = rec.get("payload")
    return p if isinstance(p, dict) else {}


# ─────────────────────────── 에이전트 ───────────────────────────


class OptimizerAgent:
    def __init__(
        self,
        mode: Mode,
        bus: Bus,
        journal_dir: Path,
        *,
        config_path: Path | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(KST),
        min_trades_for_proposal: int = 5,
        max_screening_threshold: float = 90.0,
        max_calls_per_agent: int = 50,
        poll_seconds: int = 600,
    ) -> None:
        self._mode = mode
        self._bus = bus
        self._dir = journal_dir
        self._config_path = config_path
        self._clock = clock
        self._min_trades = min_trades_for_proposal
        self._max_threshold = max_screening_threshold
        self._max_calls = max_calls_per_agent
        self._poll_seconds = poll_seconds
        self._runs = 0            # 자기 관찰 카운터

    @property
    def mode(self) -> Mode:
        return self._mode

    # ─── 1) 관찰/수집 (양 모드 공통) ───

    async def observe(self, date: str) -> ObservationReport:
        records = _read_records(self._dir / f"{date}.jsonl")
        perf = self._performance(records)
        conds = self._conditions(records)
        tokens = self._token_usage(records)
        self._runs += 1

        report = ObservationReport(
            date=date,
            mode=self._mode.value,
            performance=perf,
            conditions=conds,
            tokens=tokens,
            observed_agents=KNOWN_AGENTS,
            self_stats={
                "agent": AGENT_NAME,
                "observe_runs": self._runs,
                "journal_records": len(records),
            },
        )
        log.info(
            "meta observe %s: trades=%d win_rate=%.2f pf=%s tokens=%d",
            date, perf.trades, perf.win_rate,
            f"{perf.profit_factor:.2f}" if perf.profit_factor is not None else "n/a",
            tokens.total_calls,
        )
        await self._bus.publish(TOPIC_OBSERVATION, report)
        return report

    # ─── 2) 학습/패턴 분석 ───

    def _performance(self, records: Sequence[dict[str, Any]]) -> PerformanceReport:
        exits = [_payload(r) for r in records if r.get("topic") == "signal.exit"]
        pnls = [float(p.get("pnl_pct", 0.0)) for p in exits]
        trades = len(pnls)
        wins = [x for x in pnls if x > 0]
        losses = [x for x in pnls if x < 0]
        gross_profit = sum(wins)
        gross_loss = -sum(losses)
        win_rate = (len(wins) / trades) if trades else 0.0
        avg_win = (gross_profit / len(wins)) if wins else 0.0
        avg_loss = (gross_loss / len(losses)) if losses else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
        payoff = (avg_win / avg_loss) if avg_loss > 0 else None
        signal_accuracy = win_rate   # 진입 신호 → 비손실 비율 (단일 집중이라 trade=신호)
        return PerformanceReport(
            trades=trades, wins=len(wins), losses=len(losses), win_rate=win_rate,
            gross_profit_pct=gross_profit, gross_loss_pct=gross_loss,
            profit_factor=profit_factor, avg_win_pct=avg_win, avg_loss_pct=avg_loss,
            payoff_ratio=payoff, signal_accuracy=signal_accuracy,
        )

    def _conditions(self, records: Sequence[dict[str, Any]]) -> ConditionReport:
        ordered = sorted(records, key=lambda r: str(r.get("ts", "")))
        entry_kind: dict[str, str] = {}      # symbol → 진입 신호 종류
        sym_theme: dict[str, str] = {}       # symbol → 대표 테마

        by_entry: dict[str, list[float]] = defaultdict(list)
        by_exit: dict[str, list[float]] = defaultdict(list)
        by_theme: dict[str, list[float]] = defaultdict(list)

        for rec in ordered:
            topic = rec.get("topic")
            p = _payload(rec)
            if topic == "screening.candidates":
                themes = p.get("themes") or []
                if themes:
                    sym_theme[p.get("code", "")] = themes[0]
            elif topic == "order.event" and p.get("side") == "buy":
                sig = (p.get("approved") or {}).get("entry_signal") or {}
                entry_kind[p.get("symbol", "")] = sig.get("signal", "UNKNOWN")
            elif topic == "signal.exit":
                sym = p.get("symbol", "")
                pnl = float(p.get("pnl_pct", 0.0))
                by_entry[entry_kind.get(sym, "UNKNOWN")].append(pnl)
                by_exit[p.get("kind", "UNKNOWN")].append(pnl)
                by_theme[sym_theme.get(sym, "NONE")].append(pnl)

        return ConditionReport(
            by_entry_signal=_to_stats(by_entry),
            by_exit_kind=_to_stats(by_exit),
            by_theme=_to_stats(by_theme),
        )

    # ─── 4) 토큰 최적화 ───

    def _token_usage(self, records: Sequence[dict[str, Any]]) -> TokenUsageReport:
        calls = [_payload(r) for r in records if r.get("topic") == TOPIC_CLAUDE_CALL]
        by_agent_route: dict[str, Counter[str]] = defaultdict(Counter)
        by_agent_purpose: dict[str, Counter[str]] = defaultdict(Counter)
        agent_calls: Counter[str] = Counter()
        seen: Counter[tuple[str, str, str]] = Counter()
        findings: list[str] = []

        for c in calls:
            agent = str(c.get("agent", "unknown"))
            route = str(c.get("route", "unknown"))
            purpose = str(c.get("purpose", "unspecified"))
            agent_calls[agent] += 1
            by_agent_route[agent][route] += 1
            by_agent_purpose[agent][purpose] += 1

            # 위반: 매매 결정을 LLM에 위임 (§15.4)
            if purpose == "decision":
                findings.append(
                    f"[CRITICAL] {agent}: 매매 결정 LLM 위임 감지 (§15.4 금지) route={route}"
                )
            # 중복: 동일 (agent, route, body) 반복 호출
            key = (agent, route, _hash_body(c.get("body")))
            seen[key] += 1

        for (agent, route, _), n in seen.items():
            if n >= 2:
                findings.append(
                    f"[REDUNDANT] {agent}: 동일 호출 {n}회 반복 (route={route}) — 캐시/배치 권장"
                )
        for agent, n in agent_calls.items():
            if n > self._max_calls:
                findings.append(
                    f"[HIGH_VOLUME] {agent}: 호출 {n}회 > 임계 {self._max_calls} — 폴링 주기 완화 권장"
                )

        stats = tuple(
            AgentTokenStat(
                agent=agent,
                calls=agent_calls[agent],
                by_route=dict(by_agent_route[agent]),
                by_purpose=dict(by_agent_purpose[agent]),
            )
            for agent in sorted(agent_calls)
        )
        return TokenUsageReport(
            total_calls=sum(agent_calls.values()),
            by_agent=stats,
            waste_findings=tuple(findings),
        )

    # ─── 3) 진화/최적화 제안 (paper 전용) ───

    async def propose(self, report: ObservationReport) -> list[OptimizationProposal]:
        if self._mode != Mode.PAPER:
            log.info("meta: live 모드 — 관찰/수집만, 제안 생략 (§11)")
            return []

        proposals: list[OptimizationProposal] = []
        proposals.extend(self._strategy_proposals(report))
        proposals.extend(self._token_proposals(report))

        for p in proposals:
            await self._bus.publish(TOPIC_PROPOSAL, p)
            log.info("meta proposal %s (%s): %s", p.proposal_id, p.kind, p.rationale)
        return proposals

    def _strategy_proposals(
        self, report: ObservationReport,
    ) -> list[OptimizationProposal]:
        perf = report.performance
        if perf.trades < self._min_trades:
            return []
        out: list[OptimizationProposal] = []

        # 규칙 A: 손익비(profit_factor) < 1.0 → 스크리닝 임계 상향(더 선별적)
        if perf.profit_factor is not None and perf.profit_factor < 1.0:
            cur = self._current_value("screening.threshold")
            if cur is not None:
                new = min(float(cur) + 5.0, self._max_threshold)
                if new > float(cur):
                    out.append(self._make_proposal(
                        kind="strategy",
                        rationale=(
                            f"손익비 {perf.profit_factor:.2f} < 1.0 "
                            f"({perf.trades}건). 진입 기준을 높여 질을 개선."
                        ),
                        changes=(ParamChange(
                            key="screening.threshold", from_value=cur, to_value=new,
                            reason="저성과 구간 — 후보 선별 강화",
                        ),),
                        evidence={
                            "profit_factor": perf.profit_factor,
                            "win_rate": perf.win_rate, "trades": perf.trades,
                        },
                    ))

        # (규칙 B 타임스톱 과다 제안은 제거됨 — 타임스톱 자체가 폐지되어 더는 발생하지 않는다.)
        return out

    def _token_proposals(
        self, report: ObservationReport,
    ) -> list[OptimizationProposal]:
        findings = report.tokens.waste_findings
        if not findings:
            return []
        return [self._make_proposal(
            kind="token",
            rationale=f"토큰 사용 비효율/위반 {len(findings)}건 탐지 — 호출 패턴 개선 권장.",
            recommendations=findings,
            evidence={"total_calls": report.tokens.total_calls},
        )]

    # ─── 헬퍼 ───

    def _make_proposal(
        self,
        *,
        kind: str,
        rationale: str,
        changes: tuple[ParamChange, ...] = (),
        recommendations: tuple[str, ...] = (),
        evidence: dict[str, Any] | None = None,
    ) -> OptimizationProposal:
        return OptimizationProposal(
            proposal_id=uuid.uuid4().hex[:12],
            kind=kind,
            mode=self._mode.value,
            created_ts=self._clock().isoformat(),
            rationale=rationale,
            changes=changes,
            recommendations=recommendations,
            evidence=evidence or {},
        )

    def _current_value(self, dotted_key: str) -> Any:
        if self._config_path is None or not self._config_path.exists():
            return None
        import yaml
        doc = yaml.safe_load(self._config_path.read_text(encoding="utf-8"))
        node: Any = doc
        for seg in dotted_key.split("."):
            if not isinstance(node, dict) or seg not in node:
                return None
            node = node[seg]
        return node

    # ─── 루프 ───

    async def run_once(
        self, date: str | None = None,
    ) -> tuple[ObservationReport, list[OptimizationProposal]]:
        date = date or self._clock().strftime("%Y%m%d")
        report = await self.observe(date)
        proposals = await self.propose(report)
        return report, proposals

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("meta optimizer run failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_seconds)
            except asyncio.TimeoutError:
                continue


# ─────────────────────────── 통계 헬퍼 ───────────────────────────


def _to_stats(groups: dict[str, list[float]]) -> tuple[ConditionStat, ...]:
    out: list[ConditionStat] = []
    for key, pnls in groups.items():
        n = len(pnls)
        wins = sum(1 for x in pnls if x > 0)
        out.append(ConditionStat(
            key=key, trades=n, wins=wins,
            win_rate=(wins / n) if n else 0.0,
            avg_pnl_pct=(sum(pnls) / n) if n else 0.0,
        ))
    return tuple(sorted(out, key=lambda s: s.trades, reverse=True))


def _hash_body(body: Any) -> str:
    try:
        return json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(body)


# ─────────────────────────── YAML 적용기 (CEO 승인 시 호출) ───────────────────────────


def set_yaml_scalar(text: str, dotted_key: str, new_value: Any) -> tuple[str, bool]:
    """주석을 보존하며 점(.) 경로의 **스칼라 leaf** 값만 교체한다.

    매핑/리스트 노드는 교체하지 않는다(``(text, False)`` 반환). 들여쓰기 기반으로
    중첩 키를 추적하므로 ``strategy_params.yaml``의 주석을 손상시키지 않는다.
    """
    lines = text.split("\n")
    segs = dotted_key.split(".")
    lo, hi = 0, len(lines)
    parent_indent = -1
    seg_indent = 0
    target = -1

    for depth, seg in enumerate(segs):
        found = -1
        i = lo
        while i < hi:
            line = lines[i]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                i += 1
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= parent_indent:
                break   # 부모 블록을 벗어남
            m = re.match(r"^(\s*)([^:\s]+)\s*:(.*)$", line)
            if m and m.group(2) == seg:
                found = i
                seg_indent = indent
                break
            i += 1
        if found < 0:
            return text, False

        if depth == len(segs) - 1:
            target = found
            break

        # 다음 깊이의 블록 범위 계산
        lo = found + 1
        j = lo
        block_end = hi
        while j < hi:
            l2 = lines[j]
            s2 = l2.strip()
            if s2 and not s2.startswith("#"):
                ind2 = len(l2) - len(l2.lstrip())
                if ind2 <= seg_indent:
                    block_end = j
                    break
            j += 1
        hi = block_end
        parent_indent = seg_indent

    if target < 0:
        return text, False

    m = re.match(r"^(\s*[^:\s]+\s*:\s*)([^#\n]*?)(\s*#.*)?$", lines[target])
    if not m:
        return text, False
    old_scalar = m.group(2).strip()
    if old_scalar == "" or old_scalar.startswith("["):
        # 매핑(자식 보유) 또는 리스트 → 스칼라 교체 금지
        return text, False

    prefix, comment = m.group(1), m.group(3) or ""
    lines[target] = f"{prefix}{_format_scalar(new_value)}{comment}"
    return "\n".join(lines), True


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    return str(value)


def _format_value(value: Any) -> str:
    """스칼라 또는 인라인 리스트를 YAML 텍스트로 직렬화."""
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_format_scalar(v) for v in value) + "]"
    return _format_scalar(value)


def set_yaml_leaf(text: str, dotted_key: str, new_value: Any) -> tuple[str, bool]:
    """주석을 보존하며 점(.) 경로의 leaf 값을 교체한다(스칼라 **또는 인라인 리스트**).

    ``set_yaml_scalar``의 확장판. 매핑(자식 보유) 노드는 교체하지 않으며, 기존 leaf가
    스칼라면 스칼라로, 인라인 리스트(``[a, b]``)면 리스트로 교체할 수 있다. RSI 진입
    구간(``signal.rsi.entry_zone``)·익절 범위(``take_profit.step1.pct_range``) 같은
    리스트 파라미터를 상담/노션/복기에서 갱신하기 위해 쓴다(§13 화이트리스트는 호출자 책임).
    """
    lines = text.split("\n")
    segs = dotted_key.split(".")
    lo, hi = 0, len(lines)
    parent_indent = -1
    seg_indent = 0
    target = -1

    for depth, seg in enumerate(segs):
        found = -1
        i = lo
        while i < hi:
            line = lines[i]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                i += 1
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= parent_indent:
                break
            m = re.match(r"^(\s*)([^:\s]+)\s*:(.*)$", line)
            if m and m.group(2) == seg:
                found = i
                seg_indent = indent
                break
            i += 1
        if found < 0:
            return text, False

        if depth == len(segs) - 1:
            target = found
            break

        lo = found + 1
        j = lo
        block_end = hi
        while j < hi:
            l2 = lines[j]
            s2 = l2.strip()
            if s2 and not s2.startswith("#"):
                ind2 = len(l2) - len(l2.lstrip())
                if ind2 <= seg_indent:
                    block_end = j
                    break
            j += 1
        hi = block_end
        parent_indent = seg_indent

    if target < 0:
        return text, False

    m = re.match(r"^(\s*[^:\s]+\s*:\s*)([^#\n]*?)(\s*#.*)?$", lines[target])
    if not m:
        return text, False
    old_leaf = m.group(2).strip()
    if old_leaf == "":
        # 매핑(자식 보유) → leaf 교체 금지.
        return text, False

    prefix, comment = m.group(1), m.group(3) or ""
    lines[target] = f"{prefix}{_format_value(new_value)}{comment}"
    return "\n".join(lines), True


def apply_proposal_to_file(
    proposal: OptimizationProposal | Any, config_path: Path,
) -> list[ParamChange]:
    """CEO 승인 시에만 호출. 화이트리스트 스칼라 키만 strategy_params.yaml에 반영.

    하드리밋 파일은 절대 대상이 아니다(§4/§13). 본 함수는 모드 검증을 하지 않으며,
    paper 한정/락 검증은 호출자(CeoAgent.approve_proposal)가 책임진다.
    """
    if "hard_limits" in config_path.name:
        raise ValueError("하드리밋은 제안/자동변경 대상이 아니다 (§4, §13.1)")

    changes = tuple(getattr(proposal, "changes", ()) or ())
    if not changes:
        return []

    text = config_path.read_text(encoding="utf-8")
    applied: list[ParamChange] = []
    for ch in changes:
        key = getattr(ch, "key", None)
        to_value = getattr(ch, "to_value", None)
        if key not in TUNABLE_KEYS:
            log.warning("meta apply: 화이트리스트 외 키 거부 %s", key)
            continue
        # leaf 교체(스칼라 + 인라인 리스트). RSI 구간/익절 범위 같은 리스트도 지원.
        text, ok = set_yaml_leaf(text, key, to_value)
        if ok:
            applied.append(ch)
        else:
            log.warning("meta apply: 스칼라 교체 실패 %s", key)

    if applied:
        config_path.write_text(text, encoding="utf-8")
    return applied
