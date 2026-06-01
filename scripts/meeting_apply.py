"""회의 내용 → 실행 항목 추출·적용·이력·롤백 (CLAUDE.md §24, §3.3, §4, §11, §13).

traidair HTS 💬 상담 탭의 "📋 회의 내용 적용" 버튼 백엔드. 전체 팀 회의(🤝) 또는 1:1
상담이 끝나면 대화에서 **실행 가능한 전략 변경**을 자동 추출하고, 운영자가 선택한 항목만
``strategy_params.yaml`` 에 실제 반영한다. 모든 적용은 ``StrategyEditor`` 단일 진입점을
거친다(화이트리스트 → 주석 보존 leaf 교체 → 재읽기 검증 → git 자동 커밋 → ``ImprovementLog``).
회의 단위 결정은 ``MeetingDecisionLog`` (``data/memory/meeting_decisions.json``)에 누적된다.

4가지 액션(``--action``), 입력 페이로드는 stdin JSON(history 제외):

  extract   회의 레코드 → 추출된 변경 체크리스트(현재값·제안값·적용가능·잠금·하드리밋)
  apply     {meeting_id, meeting_q, items:[{key,value,label,reason}]} → 실제 적용 + 기록
  history   회의 적용 이력 타임라인 + 효과 verdict + 롤백 후보(입력 없음)
  rollback  {id} → 해당 결정을 from 값으로 되돌림(paper 전용)

규칙:
- **paper 모드에서만 적용**(§3.3 잠금). live면 모든 항목 ``locked``, 적용 거부.
- 하드리밋(``hard_limits.yaml``, §4) 키는 추출돼도 ``applicable:false`` + 🔒 잠금 표시.
- 화이트리스트(``TUNABLE_KEYS``, §13) 밖 키는 적용 불가.
- 결과 JSON 을 stdout 한 줄(또는 들여쓰기)로 출력(traidair 파싱). stdout UTF-8 강제.
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

from agents.meta.optimizer.main import TUNABLE_KEYS  # noqa: E402
from core.consult import extract_changes  # noqa: E402
from core.kis_client import KisClientConfig  # noqa: E402
from core.memory import ImprovementLog, MeetingDecisionLog  # noqa: E402
from core.strategy import StrategyEditor  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parents[1]
MEMORY_DIR = ROOT / "data" / "memory"
JOURNAL_DIR = ROOT / "data" / "journal"
CONFIG_PATH = ROOT / "config" / "strategy_params.yaml"

# 화이트리스트 키의 화면용 한글 라벨(없으면 키 그대로).
KEY_LABELS: dict[str, str] = {
    "screening.threshold": "스크리닝 통과 점수",
    "signal.rsi.entry_zone": "RSI 진입 구간",
    "signal.rsi.overbought": "RSI 과매수 상단",
    "signal.volume_surge_multiplier": "거래량 급증 배수",
    "stop_loss.hard_max_pct": "하드 손절(%)",
    "stop_loss.technical_stop_enabled": "기술적 손절 사용",
    "stop_loss.technical_buffer_pct": "기술적 손절 버퍼(%)",
    "stop_loss.signal_breakdown_grace_minutes": "신호붕괴 유예(분)",
    "take_profit.step1.close_ratio": "1차 익절 비율",
    "take_profit.step1.pct_range": "1차 익절 목표 범위",
    "take_profit.step2.close_ratio": "2차 익절 비율",
    "take_profit.step2.pct_range": "2차 익절 목표 범위",
    "take_profit.step3_trailing.trail_from_high_pct": "트레일링 이탈폭",
    "time_stop.enabled": "타임스톱 사용",
    "time_stop.evaluation_minutes": "타임스톱 평가 시간(분)",
    "time_stop.min_profit_pct": "타임스톱 수익 기준(%)",
    "time_stop.action": "타임스톱 동작(절반/전량)",
    "time_stop.first_check_minutes": "1차 타임스톱 시간(분)",
    "time_stop.first_check_action": "1차 타임스톱 동작",
    "time_stop.first_check_min_profit_pct": "1차 타임스톱 수익 기준(%)",
    "time_stop.flat_box_pct": "타임스톱 무방향 박스(%)",
}

# 하드리밋(config/hard_limits.yaml, §4) 키 — 추출돼도 변경 불가(🔒 잠금 표시).
HARD_LIMIT_LABELS: dict[str, str] = {
    "max_concurrent_positions": "동시 보유 종목 수 (HL-01)",
    "consecutive_stoploss_threshold": "연속 손절 임계 (HL-02)",
    "cooldown_after_stoploss_minutes": "연속 손절 쿨다운(분) (HL-02)",
    "entry_blackout_windows": "진입 금지 시간대 (HL-03/04)",
    "max_slippage_ticks": "슬리피지 가드(틱) (HL-05)",
    "margin_maintenance_buffer_pct": "담보유지비율 버퍼 (HL-06)",
    "daily_loss_halt_pct": "일일 손실 halt (§4.1 금지)",
    "max_position_pct_per_symbol": "1종목 비중 상한 (§4.1 없음)",
}


def _emit(data: dict, *, indent: int | None = None) -> int:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=indent))
    sys.stdout.flush()
    return 0


def _read_stdin() -> str:
    try:
        return sys.stdin.buffer.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return (sys.stdin.read() or "").strip()


def _is_num(p: str) -> bool:
    try:
        float(p.strip())
        return True
    except ValueError:
        return False


def coerce_value(raw: Any) -> Any:
    """문자열/숫자/리스트를 yaml 스칼라·인라인 리스트 값으로 변환.

    "[55, 65]" / "55,65" / "55~65" / "55-65" → [55, 65], "0.02" → 0.02, "30" → 30.
    """
    if isinstance(raw, (list, tuple)):
        return [coerce_value(v) for v in raw]
    if isinstance(raw, (int, float, bool)):
        return raw
    s = str(raw).strip()
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    # 구간 표기 "55~65" / "55-65" / "55 에서 65" → 리스트.
    # "-" 는 음수(예 "-2")와 충돌하나, 빈 조각을 거른 뒤 정확히 두 양수 조각일 때만
    # 구간으로 본다("-2"→["2"] len1 → 단일 음수로 폴백).
    for sep in ("~", "–", "—", "에서", "부터", "-"):
        if sep in s and "[" not in s:
            parts = [p.strip() for p in s.replace("부터", "에서").split(sep) if p.strip()]
            if len(parts) == 2 and all(_is_num(p) for p in parts):
                return [coerce_value(p) for p in parts]
    if s.startswith("[") or ("," in s and all(_is_num(p) for p in s.strip("[]").split(","))):
        return [coerce_value(p) for p in s.strip("[]").split(",") if p.strip()]
    if _is_num(s):
        if "." not in s and "e" not in low:
            try:
                return int(s)
            except ValueError:
                pass
        try:
            return float(s)
        except ValueError:
            pass
    return s


def _fmt(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    return str(value)


def _yaml_value(key: str) -> Any:
    import yaml
    try:
        doc = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    node: Any = doc
    for seg in key.split("."):
        if not isinstance(node, dict) or seg not in node:
            return None
        node = node[seg]
    return node


def _meeting_text(record: dict) -> str:
    """회의 레코드의 자유 텍스트(자연어 추출용)를 한 덩어리로."""
    parts: list[str] = [str(record.get("q") or ""), str(record.get("ceo") or ""),
                        str(record.get("interim") or "")]
    for st in record.get("statements") or []:
        if isinstance(st, dict):
            parts.append(str(st.get("text") or st.get("a") or ""))
        else:
            parts.append(str(st))
    for db in record.get("debate") or []:
        if isinstance(db, dict):
            parts.append(str(db.get("text") or db.get("a") or ""))
        else:
            parts.append(str(db))
    return "\n".join(p for p in parts if p)


# ─────────────────────────── 액션: extract ───────────────────────────

def action_extract(record: dict, mode: str) -> dict:
    """회의 레코드 → 적용 가능 변경 체크리스트. 절대 yaml 을 건드리지 않는다.

    실패/빈 경우는 ``ok:true`` + 구체적 ``message`` 로 돌려준다(UI 가 회색 안내로 표시).
    예외는 main() 의 try/except 가 ``파싱 오류`` 로 잡는다.
    """
    live = mode != "paper"
    free_text = _meeting_text(record).strip()
    n_imp = sum(
        1 for i in (record.get("improvements") or [])
        if isinstance(i, dict) and str(i.get("key") or "").strip()
    )
    # 회의 내용 자체가 비어 있으면(대화·합의 개선안 모두 없음) 명확히 알린다.
    if not free_text and not n_imp:
        return {
            "ok": True, "mode": mode, "count": 0, "applicable": 0,
            "items": [], "empty": True,
            "message": "회의 내용이 비어 있어요 — 추출할 대화나 합의된 개선안이 없어요. "
                       "회의를 진행한 뒤 다시 눌러 주세요.",
        }
    # 1) 구조화 개선안([[PARAM]] → record.improvements) 우선.
    candidates: dict[str, dict] = {}
    for imp in record.get("improvements") or []:
        if not isinstance(imp, dict):
            continue
        key = str(imp.get("key") or "").strip()
        if not key:
            continue
        candidates[key] = {
            "key": key,
            "value": coerce_value(imp.get("val", imp.get("value"))),
            "reason": str(imp.get("reason") or "회의 합의"),
            "origin": "meeting",
        }
    # 2) 자유 대화에서 규칙 기반 추출(미구조화 발화 보완) — 이미 있는 키는 덮어쓰지 않음.
    for sug in extract_changes(_meeting_text(record)):
        if sug.key not in candidates:
            candidates[sug.key] = {
                "key": sug.key, "value": sug.value,
                "reason": f"대화 추출: {sug.reason}", "origin": "text",
            }

    items: list[dict] = []
    for c in candidates.values():
        key, value = c["key"], c["value"]
        label = KEY_LABELS.get(key, HARD_LIMIT_LABELS.get(key, key))
        current = _yaml_value(key)
        item = {
            "key": key, "label": label, "proposed": value, "current": current,
            "reason": c["reason"], "origin": c["origin"],
            "applicable": False, "locked": False, "hard_limit": False, "note": "",
            "display": f"{label}: {_fmt(current) if current is not None else '?'} → {_fmt(value)}",
        }
        if key in HARD_LIMIT_LABELS or key not in TUNABLE_KEYS:
            item["hard_limit"] = key in HARD_LIMIT_LABELS
            item["note"] = ("🔒 하드리밋 — 변경 불가 (§4)" if key in HARD_LIMIT_LABELS
                            else "🔒 보호된 키 — 변경 불가 (§13 화이트리스트 밖)")
        elif _same(current, value):
            item["note"] = "이미 같은 값이에요"
        elif live:
            item["locked"] = True
            item["note"] = "🔒 실전 모드 — 모의에서만 적용"
        else:
            item["applicable"] = True
        items.append(item)

    items.sort(key=lambda i: (not i["applicable"], i["label"]))
    n_ok = sum(1 for i in items if i["applicable"])
    return {
        "ok": True, "mode": mode, "count": len(items), "applicable": n_ok,
        "items": items, "empty": False,
        "message": (f"실행 항목 {len(items)}개를 찾았어요 (적용 가능 {n_ok}개)."
                    if items else
                    "회의 내용은 있는데 바꿀 만한 전략 수치를 못 찾았어요. "
                    "예: 'RSI 기준 55~65로', '타임스톱 20분으로'처럼 수치를 말해 주세요."),
    }


# ─────────────────────────── 액션: apply ───────────────────────────

def action_apply(payload: dict, editor: StrategyEditor, mode: str,
                 ts: str, date: str) -> dict:
    items = payload.get("items") or []
    if not items:
        return {"ok": False, "reason": "적용할 항목이 없어요. 체크박스로 항목을 선택해 주세요."}
    if mode != "paper":
        return {"ok": False, "locked": True, "applied": [], "failed": [],
                "reason": "🔒 실전 모드에서는 전략 수정 불가 (§3.3)"}

    mlog = MeetingDecisionLog.load(MEMORY_DIR)
    meeting_id = str(payload.get("meeting_id") or ts)
    meeting_q = str(payload.get("meeting_q") or "회의")
    meeting_ts = str(payload.get("meeting_ts") or ts)
    applied, failed = [], []
    for it in items:
        key = str(it.get("key") or "").strip()
        label = str(it.get("label") or KEY_LABELS.get(key, key))
        value = coerce_value(it.get("value"))
        reason = str(it.get("reason") or "회의 결정")
        res = editor.apply(
            key, value, ts=ts, date=date, source="meeting",
            reason=f"회의: {reason}", label=label,
        )
        if res.ok:
            dec = mlog.record(
                ts=ts, date=date, meeting_id=meeting_id, meeting_q=meeting_q,
                meeting_ts=meeting_ts, key=res.key, label=label,
                from_value=res.before, to_value=res.after, reason=reason,
                source="meeting", commit=res.commit, improvement_id=res.improvement_id,
            )
            d = res.to_dict()
            d["decision_id"] = dec.id
            applied.append(d)
        else:
            failed.append(res.to_dict())
    return {
        "ok": bool(applied), "mode": mode, "applied": applied, "failed": failed,
        "message": (("✅ 회의 결정 " + str(len(applied)) + "건 적용 완료 — "
                     + " · ".join(a["display"] for a in applied))
                    if applied else "변경을 적용하지 못했어요."),
    }


# ─────────────────────────── 액션: history ───────────────────────────

def action_history() -> dict:
    mlog = MeetingDecisionLog.load(MEMORY_DIR)
    ilog = ImprovementLog.load(MEMORY_DIR)
    # 효과 verdict 채우기(저널 전후 비교) — 저널 없으면 unknown.
    try:
        ilog.evaluate_effects(JOURNAL_DIR)
    except Exception:  # noqa: BLE001 — 효과 평가 실패는 비치명(이력은 그대로 보여준다)
        pass
    eff = {e.id: e for e in ilog.entries}

    timeline = mlog.timeline()
    rollback_candidates: list[dict] = []
    for d in timeline:
        e = eff.get(d.get("improvement_id"))
        if e is not None:
            d["verdict"] = e.verdict
            d["before_pnl"] = e.before_pnl
            d["after_pnl"] = e.after_pnl
            d["after_trades"] = e.after_trades
        else:
            d["verdict"] = "unknown"
        # 롤백 후보: 효과 악화 + 아직 안 되돌림 + 롤백 자신이 아님.
        if (d.get("verdict") == "worse" and not d.get("rolled_back")
                and d.get("source") == "meeting"):
            rollback_candidates.append({
                "id": d["id"], "key": d["key"], "label": d.get("label", d["key"]),
                "from": d["from"], "to": d["to"],
                "before_pnl": e.before_pnl, "after_pnl": e.after_pnl,
                "reason": (f"적용 후 평균 손익 {e.after_pnl}%(전 {e.before_pnl}%)로 악화 "
                           f"— {d.get('label', d['key'])}를 되돌리는 걸 추천해요"),
            })
    return {
        "ok": True, "total": len(timeline), "decisions": timeline,
        "rollback_candidates": rollback_candidates,
    }


# ─────────────────────────── 액션: rollback ───────────────────────────

def action_rollback(payload: dict, editor: StrategyEditor, mode: str,
                    ts: str, date: str) -> dict:
    decision_id = str(payload.get("id") or "").strip()
    if not decision_id:
        return {"ok": False, "reason": "롤백할 결정 id가 없어요."}
    if mode != "paper":
        return {"ok": False, "locked": True,
                "reason": "🔒 실전 모드에서는 롤백(전략 수정) 불가 (§3.3)"}
    mlog = MeetingDecisionLog.load(MEMORY_DIR)
    dec = mlog.find(decision_id)
    if dec is None:
        return {"ok": False, "reason": f"결정을 찾을 수 없어요: {decision_id}"}
    if dec.rolled_back:
        return {"ok": False, "reason": "이미 되돌린 결정이에요."}
    # to → from 으로 되돌린다.
    res = editor.apply(
        dec.key, dec.from_value, ts=ts, date=date, source="meeting-rollback",
        reason=f"회의 결정 롤백: {dec.label} {_fmt(dec.to_value)}→{_fmt(dec.from_value)}",
        label=dec.label,
    )
    if not res.ok:
        return {"ok": False, "reason": res.reason}
    mlog.mark_rolled_back(decision_id)
    mlog.record(
        ts=ts, date=date, meeting_id=dec.meeting_id, meeting_q=dec.meeting_q,
        meeting_ts=dec.meeting_ts, key=dec.key, label=dec.label,
        from_value=res.before, to_value=res.after, reason="롤백",
        source="meeting-rollback", commit=res.commit,
        improvement_id=res.improvement_id, rollback_of=decision_id,
    )
    out = res.to_dict()
    out["message"] = "↩️ 되돌렸어요 — " + res.display
    return out


# ─────────────────────────── 진입점 ───────────────────────────

def _same(a: Any, b: Any) -> bool:
    import math
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)
    return a == b


def main() -> int:
    parser = argparse.ArgumentParser(description="회의 내용 추출·적용·이력·롤백")
    parser.add_argument("--action", required=True,
                        choices=["extract", "apply", "history", "rollback"])
    args = parser.parse_args()

    now = datetime.now(KST)
    ts, date = now.isoformat(), now.strftime("%Y%m%d")

    try:
        cfg = KisClientConfig.from_files(project_root=ROOT)
        mode = cfg.mode.value
    except Exception as exc:  # noqa: BLE001
        return _emit({"ok": False, "reason": f"설정 로드 실패: {exc}"[:200]})

    # 어떤 액션도 예외로 빈 stdout 을 남기지 않는다 — 크래시하면 traidair UI 가 사유를
    # 못 받아 "알 수 없음"으로 표시하던 버그 차단. 모든 예외를 구체적 사유로 emit 한다.
    import traceback

    def _guard(fn) -> int:
        try:
            return _emit(fn())
        except Exception as exc:  # noqa: BLE001
            return _emit({
                "ok": False,
                "reason": f"파싱 오류: {type(exc).__name__}: {exc}"[:200],
                "detail": traceback.format_exc()[-500:],
            })

    if args.action == "history":
        return _guard(action_history)

    payload: dict = {}
    if args.action in ("extract", "apply", "rollback"):
        raw = _read_stdin()
        if raw:
            try:
                payload = json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                return _emit({"ok": False,
                              "reason": f"파싱 오류: 입력 JSON 형식이 아니에요 — {exc}"[:200]})
        if not isinstance(payload, dict):
            return _emit({"ok": False, "reason": "입력은 JSON object 여야 해요."})

    if args.action == "extract":
        return _guard(lambda: action_extract(payload, mode))

    editor = StrategyEditor(
        config_path=CONFIG_PATH, memory_dir=MEMORY_DIR, project_root=ROOT, mode=mode,
    )
    if args.action == "apply":
        return _guard(lambda: action_apply(payload, editor, mode, ts, date))
    if args.action == "rollback":
        return _guard(lambda: action_rollback(payload, editor, mode, ts, date))
    return _emit({"ok": False, "reason": "알 수 없는 액션"})


if __name__ == "__main__":
    raise SystemExit(main())
