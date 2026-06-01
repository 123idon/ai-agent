"""전략 파라미터 단일 적용 진입점 (CLAUDE.md §2.7, §3.3, §11, §13).

상담(💬)·복기(학습)·노션(§23)에서 도출된 변경을 **실제 파일에 반영**하는 유일한
choke point. 기존 ``apply_proposal_to_file``/``set_yaml_leaf``(주석 보존·화이트리스트)
위에 다음을 한 번에 묶는다:

  a. 모드 게이트   : live면 잠금(§3.3) — 적용 거부
  b. 화이트리스트  : ``TUNABLE_KEYS`` 밖이면 거부(§4·§13 하드리밋/구조 보호)
  c. 파일 수정     : ``set_yaml_leaf`` (스칼라 + 인라인 리스트, 주석 보존)
  d. 재읽기 검증   : 다시 yaml 파싱해 값이 실제로 바뀌었는지 확인
  e. git 자동커밋  : 검증 성공 시 ``git add`` + ``git commit``
  f. 영구 기록     : ``ImprovementLog`` 에 전후 값/출처/사유 누적(세션 간 기억)

핵심 변경 전후 값을 ``StrategyApplyResult.before``/``after`` 로 돌려주므로 호출자가
"RSI 기준: 50~65 → 55~65로 변경됨" 같은 화면 표시를 만들 수 있다.
"""
from __future__ import annotations

import logging
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agents.meta.optimizer.main import (
    TUNABLE_KEYS,
    OptimizationProposal,
    ParamChange,
    apply_proposal_to_file,
)
from core.memory.improvement_log import ImprovementLog

log = logging.getLogger(__name__)


# ── funnel/진입 파라미터 안전범위 (CLAUDE.md §19 죽음의 나선 방지) ──
# consult/auto-learn 자동튜닝이 "연속 손절"에 반응해 진입조건을 **단조 강화** → 후보 funnel을
# 0으로 굶겨 거래가 사실상 멈추는 회귀(2026-06-01: screening.threshold 70→80→85)를 차단한다.
# 범위 밖 요청은 거부가 아니라 **가장 가까운 경계로 보정(clamp)** 후 적용한다(파이프라인은 계속
# 돌되 funnel을 굶기지 못함). 경계값은 CLAUDE.md SSOT(§5.1 threshold=70 등) 기준.
# 하드리밋(§4)이 아니라 "자동튜닝 가드레일"이며, 운영자 수동 변경도 동일하게 보정된다.
TUNE_BOUNDS_SCALAR: dict[str, tuple[float, float]] = {
    "screening.threshold": (70.0, 80.0),           # SSOT 70(§5.1). 80 초과 = funnel 굶음
    "signal.volume_surge_multiplier": (1.5, 3.0),  # 너무 높이면 진입 0
    "signal.rsi.overbought": (65.0, 80.0),         # 너무 낮추면 과매수 차단이 진입을 굶음
    "stop_loss.hard_max_pct": (-0.03, -0.015),     # §5.4 -2%. -1.5%보다 빡빡/-3%보다 느슨 금지
    "time_stop.evaluation_minutes": (10.0, 40.0),
}
# 인라인 리스트(원소별 (min,max)). entry_zone=[low,high] — low를 올려 진입창을 굶기지 못하게.
TUNE_BOUNDS_LIST: dict[str, tuple[tuple[float, float], ...]] = {
    "signal.rsi.entry_zone": ((45.0, 55.0), (60.0, 72.0)),
}


def _clamp_tune(key: str, value: Any) -> tuple[Any, str]:
    """funnel/진입 파라미터를 안전범위로 보정한다.

    ``(보정값, 한글 note)`` 반환. 보정이 없으면 ``(value, "")``. 정수 입력은 정수로 유지한다.
    """
    if key in TUNE_BOUNDS_SCALAR and isinstance(value, (int, float)) \
            and not isinstance(value, bool):
        lo, hi = TUNE_BOUNDS_SCALAR[key]
        clamped = min(max(float(value), lo), hi)
        if math.isclose(clamped, float(value), rel_tol=1e-9, abs_tol=1e-12):
            return value, ""
        if isinstance(value, int):
            clamped = int(round(clamped))
        return clamped, (f" (요청 {_fmt(value)} → 안전범위 [{_fmt(lo)}, {_fmt(hi)}]로 "
                         f"보정, §19 죽음의 나선 방지)")
    if key in TUNE_BOUNDS_LIST and isinstance(value, (list, tuple)):
        bounds = TUNE_BOUNDS_LIST[key]
        if len(value) != len(bounds):
            return value, ""
        out: list[Any] = []
        changed = False
        for v, (lo, hi) in zip(value, bounds):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                c = min(max(float(v), lo), hi)
                if not math.isclose(c, float(v), rel_tol=1e-9, abs_tol=1e-12):
                    changed = True
                out.append(int(round(c)) if isinstance(v, int) else c)
            else:
                out.append(v)
        if not changed:
            return value, ""
        return out, f" (요청 {_fmt(value)} → 안전범위로 보정, §19 죽음의 나선 방지)"
    return value, ""


@dataclass
class StrategyApplyResult:
    ok: bool
    key: str
    before: Any = None
    after: Any = None
    reason: str = ""
    committed: bool = False
    commit: str | None = None
    locked: bool = False
    display: str = ""              # "RSI 기준: [50, 65] → [55, 65]로 변경됨"
    improvement_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "key": self.key, "from": self.before, "to": self.after,
            "reason": self.reason, "committed": self.committed, "commit": self.commit,
            "locked": self.locked, "display": self.display,
            "improvement_id": self.improvement_id,
        }


@dataclass
class StrategyEditor:
    config_path: Path
    memory_dir: Path
    project_root: Path
    mode: str = "paper"            # paper | live (live는 잠금)
    git_commit: bool = True
    improvement_log: ImprovementLog | None = field(default=None)

    def __post_init__(self) -> None:
        self.config_path = Path(self.config_path)
        self.memory_dir = Path(self.memory_dir)
        self.project_root = Path(self.project_root)
        if self.improvement_log is None:
            self.improvement_log = ImprovementLog.load(self.memory_dir)

    # ─────────────────────────── 적용 ───────────────────────────

    def apply(
        self,
        key: str,
        value: Any,
        *,
        ts: str,
        date: str,
        source: str,                # consult | review | notion | meta
        reason: str = "",
        expected_effect: str = "",
        label: str | None = None,   # 화면 표시용 한글 키 라벨(예: "RSI 기준")
    ) -> StrategyApplyResult:
        # a. 모드 게이트 (§3.3)
        if self.mode != "paper":
            return StrategyApplyResult(
                ok=False, key=key, locked=True,
                reason="🔒 실전 모드에서는 전략 수정 불가 (파라미터 잠금, §3.3)",
            )

        # b. 화이트리스트 (§4·§13)
        if key not in TUNABLE_KEYS:
            return StrategyApplyResult(
                ok=False, key=key,
                reason=f"보호된 키라 수정할 수 없어요: {key} "
                       f"(하드리밋/구조 값은 변경 금지, §4·§13).",
            )

        # b2. funnel/진입 파라미터 안전범위 클램프 (§19 죽음의 나선 방지)
        # consult/auto-learn 자동튜닝이 진입조건을 단조 강화해 거래 0으로 수렴하는 것을 차단.
        # 범위 밖이면 경계로 보정하고 사유에 명시(거부가 아니라 보정 후 적용).
        value, _clamp_note = _clamp_tune(key, value)
        if _clamp_note:
            reason = (reason + _clamp_note).strip()
            log.info("자동튜닝 가드: %s 요청을 안전범위로 보정%s", key, _clamp_note)

        before = self._yaml_value(key)
        if _same(before, value):
            return StrategyApplyResult(
                ok=False, key=key, before=before, after=value,
                reason=f"값이 이미 {value} 라 변경할 게 없어요.",
            )

        # c. 파일 수정 (주석 보존 leaf 교체)
        proposal = OptimizationProposal(
            proposal_id=source, kind="strategy", mode=self.mode, created_ts=ts,
            rationale=reason or f"{source} 적용",
            changes=(ParamChange(key=key, from_value=before, to_value=value,
                                 reason=reason or source),),
        )
        try:
            applied = apply_proposal_to_file(proposal, self.config_path)
        except Exception as exc:  # noqa: BLE001
            return StrategyApplyResult(
                ok=False, key=key, before=before,
                reason=f"❌ 적용 실패: 파일 쓰기 오류 — {exc}"[:200],
            )
        if not applied:
            return StrategyApplyResult(
                ok=False, key=key, before=before,
                reason="❌ 적용 실패: 파일에서 해당 값을 찾지 못했어요(leaf 교체 실패).",
            )

        # d. 재읽기 검증
        after = self._yaml_value(key)
        if not _same(after, value):
            return StrategyApplyResult(
                ok=False, key=key, before=before, after=after,
                reason=f"❌ 적용 실패: 파일에 반영되지 않았어요 "
                       f"(기대 {value}, 실제 {after}).",
            )

        # e. git 자동 커밋
        commit = None
        committed = False
        if self.git_commit:
            git = self._git_commit(
                f"chore(strategy): {source} 적용 — {key} {before}→{after}"
            )
            committed = git.get("committed", False)
            commit = git.get("commit")

        # f. 영구 기록 (ImprovementLog)
        assert self.improvement_log is not None
        entry = self.improvement_log.record(
            ts=ts, date=date, source=source, key=key,
            from_value=before, to_value=after, reason=reason,
            expected_effect=expected_effect, mode=self.mode, commit=commit,
        )

        disp_label = label or key
        return StrategyApplyResult(
            ok=True, key=key, before=before, after=after, reason=reason,
            committed=committed, commit=commit,
            display=f"{disp_label}: {_fmt(before)} → {_fmt(after)}로 변경됨",
            improvement_id=entry.id,
        )

    # ─────────────────────────── 내부 ───────────────────────────

    def _yaml_value(self, dotted_key: str) -> Any:
        doc = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        node: Any = doc
        for seg in dotted_key.split("."):
            if not isinstance(node, dict) or seg not in node:
                return None
            node = node[seg]
        return node

    def _git_commit(self, message: str) -> dict[str, Any]:
        rel = str(self.config_path.relative_to(self.project_root)) \
            if self.config_path.is_absolute() else str(self.config_path)
        root = str(self.project_root)
        enc = {"encoding": "utf-8", "errors": "replace"}
        try:
            subprocess.run(["git", "-C", root, "add", rel],
                           check=True, capture_output=True, text=True, timeout=20, **enc)
            diff = subprocess.run(
                ["git", "-C", root, "diff", "--cached", "--quiet", "--", rel],
                capture_output=True, text=True, timeout=20, **enc)
            if diff.returncode == 0:
                return {"committed": False, "reason": "변경 내용이 없어 커밋 생략"}
            proc = subprocess.run(
                ["git", "-C", root, "commit", "-m", message, "--", rel],
                capture_output=True, text=True, timeout=30, **enc)
            if proc.returncode != 0:
                return {"committed": False,
                        "reason": (proc.stderr or proc.stdout or "git commit 실패").strip()[:200]}
            rev = subprocess.run(
                ["git", "-C", root, "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=20, **enc)
            return {"committed": True, "commit": rev.stdout.strip()}
        except FileNotFoundError:
            return {"committed": False, "reason": "git 미설치 — 커밋 생략(파일은 정상 적용됨)"}
        except subprocess.TimeoutExpired:
            return {"committed": False, "reason": "git 응답 지연 — 커밋 생략(파일은 정상 적용됨)"}
        except Exception as exc:  # noqa: BLE001
            return {"committed": False, "reason": f"git 오류: {exc}"[:200]}


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)
    return a == b


def _fmt(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    return str(value)
