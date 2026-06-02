"""메타 에이전트 최적화 제안 실행기 (CLAUDE.md §2.7, §11).

traidair HTS의 **🧬 진화+** 버튼이 호출한다. 최근 저널(``data/journal/*.jsonl``)을
메타 ``OptimizerAgent``로 관찰(observe)해 성과·토큰 리포트를 만들고, **paper 모드에서만**
전략 진화 제안(propose)을 생성한 뒤 화이트리스트(``TUNABLE_KEYS``) 스칼라만
``config/strategy_params.yaml``에 반영한다.

규칙:
- live 모드: 관찰만 하고 파라미터는 절대 건드리지 않는다(§3.3 잠금).
- 적용 경로는 CEO 승인과 동일한 ``apply_proposal_to_file``(주석 보존·화이트리스트)만 사용.
- 결과는 ``state/evolve_result.json``에 기록 → traidair ``/api/backtest/evolve-result``로 서빙.

제안은 **자동 적용하지 않는다**(기본). traidair 제안 카드의 **✅ 적용** 버튼이
``scripts/apply_proposal.py``로 개별 제안을 적용·검수·커밋한다(§2.7 운영자 승인 흐름).

환경변수:
- EVOLVE_DAYS : 관찰할 최근 거래일 수 (기본 10)
- EVOLVE_APPLY: "1"이면 관찰 즉시 일괄 자동 적용(레거시). 기본 "0"(카드에서 수동 적용).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from agents.meta.optimizer.main import (  # noqa: E402
    OptimizerAgent,
    apply_proposal_to_file,
)
from core.kis_client import KisClientConfig, Mode  # noqa: E402
from core.messaging import Bus  # noqa: E402
from core.time_utils import KST  # noqa: E402

log = logging.getLogger(__name__)


def _recent_journal_dates(journal_dir: Path, n: int) -> list[str]:
    """data/journal/{YYYYMMDD}.jsonl 중 최신 n개 날짜(오래된→최신)."""
    if not journal_dir.exists():
        return []
    dates = sorted(
        p.stem for p in journal_dir.glob("*.jsonl") if p.stem.isdigit()
    )
    return dates[-n:]


def _write_result(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


async def _run_evolve() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    root = Path(__file__).parents[1]
    cfg = KisClientConfig.from_files(project_root=root)
    mode = cfg.mode
    result_path = root / "state" / "evolve_result.json"
    config_path = root / "config" / "strategy_params.yaml"
    journal_dir = root / "data" / "journal"

    n_days = int(os.getenv("EVOLVE_DAYS") or 10)
    do_apply = (os.getenv("EVOLVE_APPLY") or "0") != "0" and mode == Mode.PAPER

    # 진행 표시용 초기 기록(HTS가 즉시 "실행 중"을 볼 수 있도록).
    _write_result(result_path, {
        "ok": True, "running": True, "mode": mode.value,
        "startedAt": datetime.now(KST).isoformat(),
        "observed": [], "proposals": [], "applied": [], "recommendations": [],
        "message": "관찰 시작…",
    })

    dates = _recent_journal_dates(journal_dir, n_days)
    if not dates:
        _write_result(result_path, {
            "ok": False, "running": False, "mode": mode.value,
            "finishedAt": datetime.now(KST).isoformat(),
            "observed": [], "proposals": [], "applied": [], "recommendations": [],
            "message": "저널 데이터 없음 — 먼저 백테스트를 한 번 실행하세요.",
        })
        log.warning("evolve: 저널 없음")
        return 0

    bus = Bus()
    optimizer = OptimizerAgent(
        mode, bus, journal_dir, config_path=config_path,
        clock=lambda: datetime.now(KST),
    )

    observed: list[dict] = []
    all_proposals: list = []
    for d in dates:
        try:
            report = await optimizer.observe(d)
            proposals = await optimizer.propose(report)
        except Exception:  # noqa: BLE001
            log.warning("evolve observe/propose 실패 %s", d, exc_info=True)
            continue
        perf = report.performance
        observed.append({
            "date": d,
            "trades": perf.trades,
            "winRate": round(perf.win_rate * 100, 1),
            "profitFactor": (
                round(perf.profit_factor, 2)
                if perf.profit_factor is not None else None
            ),
            "tokenCalls": report.tokens.total_calls,
            "proposals": len(proposals),
        })
        all_proposals.extend(proposals)

    # 제안 직렬화 + (paper 한정) 적용.
    proposals_out: list[dict] = []
    applied_out: list[dict] = []
    recommendations: list[str] = []
    seen_changes: set[tuple[str, object]] = set()

    for p in all_proposals:
        entry = {
            "id": p.proposal_id,
            "kind": p.kind,
            "rationale": p.rationale,
            "changes": [
                {"key": c.key, "from": c.from_value, "to": c.to_value,
                 "reason": c.reason}
                for c in p.changes
            ],
            "recommendations": list(p.recommendations),
        }
        proposals_out.append(entry)
        recommendations.extend(p.recommendations)

        if do_apply and p.changes:
            try:
                applied = apply_proposal_to_file(p, config_path)
            except Exception:  # noqa: BLE001
                log.warning("evolve apply 실패 %s", p.proposal_id, exc_info=True)
                applied = []
            for c in applied:
                # 같은 키 중복 적용은 한 번만 기록(여러 날 동일 제안 방지).
                if (c.key, c.to_value) in seen_changes:
                    continue
                seen_changes.add((c.key, c.to_value))
                applied_out.append({
                    "key": c.key, "from": c.from_value, "to": c.to_value,
                    "reason": c.reason,
                })

    strategy_proposals = [p for p in proposals_out if p.get("changes")]
    if mode != Mode.PAPER:
        msg = "live 모드 — 관찰만 수행(파라미터 잠금, §3.3)."
    elif applied_out:
        msg = f"{len(applied_out)}개 파라미터를 적용했어요 (다음 백테스트부터 반영)."
    elif strategy_proposals:
        msg = f"개선 제안 {len(strategy_proposals)}개 — 카드에서 ✅ 적용을 눌러 반영하세요."
    elif proposals_out:
        msg = "권고만 있어요(적용할 전략 변경 없음)."
    else:
        msg = "현재 성과로는 바꿀 게 없어요 — 전략이 안정적이에요."

    _write_result(result_path, {
        "ok": True, "running": False, "mode": mode.value,
        "finishedAt": datetime.now(KST).isoformat(),
        "daysObserved": len(observed),
        "observed": observed,
        "proposals": proposals_out,
        "applied": applied_out,
        "recommendations": recommendations[:20],
        "message": msg,
    })
    log.info(
        "evolve 완료: 관찰 %d일, 제안 %d, 적용 %d (mode=%s)",
        len(observed), len(proposals_out), len(applied_out), mode.value,
    )
    return 0


def main() -> int:
    return asyncio.run(_run_evolve())


if __name__ == "__main__":
    sys.exit(main())
