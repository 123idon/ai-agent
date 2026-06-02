"""노션 학습 → 전략 적용 (CLAUDE.md §23·§24.5, §3.3, §4, §11, §13).

traidair HTS 💬 상담 탭 "📚 노션 학습 적용하기" 버튼의 백엔드. 학습부가 수집해 둔
``data/memory/notion_knowledge.json`` 을 읽어, 노션에 명시된 규칙 중 **전략 파라미터로
반영 가능한 것**을 체크리스트로 추출하고, 운영자가 선택한 항목만 ``strategy_params.yaml``
에 실제 반영한다. 모든 적용은 ``StrategyEditor`` 단일 진입점을 거친다(화이트리스트 →
주석 보존 leaf 교체 → 재읽기 검증 → git 자동 커밋 → ``ImprovementLog``). 적용 이력은
``data/memory/notion_applied.json`` 에 누적된다.

3가지 액션(``--action``):

  extract   notion_knowledge.json → 적용 가능 항목 체크리스트(현재값·제안값·부서·잠금)
            + 미반영 고급 규칙(R/R·VWAP·OBV·볼린저 수축·재료 강도 = "도입 가능")
  apply     stdin {items:[{key,value,label,reason}], pending:[{label,sample}]}
            → tunable 항목 실제 반영 + pending 도입 예정 등록 + 이력 기록 + 깃 커밋
  history   notion_applied.json 적용 이력(입력 없음)

규칙:
- **paper 모드에서만 적용**(§3.3). live면 모든 항목 ``locked``, 적용 거부.
- 하드리밋(§4)·화이트리스트(§13) 밖 키는 ``applicable:false`` + 🔒 표시.
- 미반영 고급 규칙은 파라미터화돼 있지 않아 yaml 로 못 바꾸므로, "도입 예정(로드맵)"
  으로만 등록한다(코드 구현 필요). 정직하게 별도 표기한다.
- 결과 JSON 을 stdout 한 줄로 출력(traidair 파싱). stdout UTF-8 강제.
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
from core.kis_client import KisClientConfig  # noqa: E402
from core.notion_client import extract_strategy_rules  # noqa: E402
from core.strategy import StrategyEditor  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parents[1]
MEMORY_DIR = ROOT / "data" / "memory"
CONFIG_PATH = ROOT / "config" / "strategy_params.yaml"
KNOWLEDGE_PATH = MEMORY_DIR / "notion_knowledge.json"
APPLIED_PATH = MEMORY_DIR / "notion_applied.json"

# 키 → (부서 id, 화면 라벨). 노션 규칙을 어느 에이전트가 흡수하는지 보여준다(§23.1).
KEY_LABELS: dict[str, str] = {
    "screening.threshold": "스크리닝 통과 점수",
    "signal.rsi.entry_zone": "RSI 진입 구간",
    "signal.rsi.overbought": "RSI 과매수 상단",
    "signal.volume_surge_multiplier": "거래량 급증 배수",
    "stop_loss.hard_max_pct": "하드 손절(%)",
    "stop_loss.signal_breakdown_grace_minutes": "신호붕괴 유예(분)",
    "take_profit.step1.close_ratio": "1차 익절 비율",
    "take_profit.step1.pct_range": "1차 익절 목표 범위",
    "take_profit.step2.close_ratio": "2차 익절 비율",
    "take_profit.step2.pct_range": "2차 익절 목표 범위",
    "take_profit.step3_trailing.trail_from_high_pct": "트레일링 이탈폭",
    # 타임스톱(시간 기반 매도)은 제거됨(§5.5) — time_stop 라벨 없음.
}


def _agent_for(key: str) -> tuple[str, str]:
    """키 접두사 → (부서 id, 한글 라벨). 노션 분류(§23.1) 매핑."""
    if key.startswith("screening"):
        return "screening", "🔎 스크리닝"
    if key.startswith("signal"):
        return "signal", "📈 신호분석"
    if key.startswith(("stop_loss", "take_profit")):
        return "risk", "🛡 리스크"
    if key.startswith("market"):
        return "market_watch", "🌐 시장상황"
    return "ceo", "👔 CEO"


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
    """문자열/숫자/리스트를 yaml 스칼라·인라인 리스트 값으로 변환(meeting_apply 와 동일)."""
    if isinstance(raw, (list, tuple)):
        return [coerce_value(v) for v in raw]
    if isinstance(raw, (int, float, bool)):
        return raw
    s = str(raw).strip()
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
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


def _same(a: Any, b: Any) -> bool:
    import math
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)
    return a == b


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


def _load_knowledge() -> dict | None:
    if not KNOWLEDGE_PATH.exists():
        return None
    try:
        return json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _load_applied() -> list[dict]:
    if not APPLIED_PATH.exists():
        return []
    try:
        data = json.loads(APPLIED_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save_applied(entries: list[dict]) -> None:
    APPLIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = APPLIED_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    import os
    os.replace(tmp, APPLIED_PATH)


# ─────────────────────────── 액션: extract ───────────────────────────

def action_extract(mode: str) -> dict:
    """notion_knowledge.json → 적용 가능 항목 + 미반영(도입 가능) 항목. yaml 무수정."""
    data = _load_knowledge()
    if not data:
        return {
            "ok": True, "synced": False, "count": 0, "applicable": 0,
            "items": [], "pending": [],
            "message": "아직 노션을 동기화하지 않았어요. 먼저 🔄 지금 업데이트로 "
                       "노션을 읽어 온 뒤 적용해 주세요.",
        }

    live = mode != "paper"
    suggestions, pending = extract_strategy_rules(data)

    items: list[dict] = []
    for s in suggestions:
        key, value = s.key, coerce_value(s.value)
        agent_id, agent_label = _agent_for(key)
        label = KEY_LABELS.get(key, getattr(s, "label", key) or key)
        current = _yaml_value(key)
        item = {
            "key": key, "label": label, "proposed": value, "current": current,
            "reason": getattr(s, "reason", "") or "노션 학습 반영",
            "agent": agent_id, "agent_label": agent_label, "kind": "param",
            "applicable": False, "locked": False, "hard_limit": False, "note": "",
            "display": (f"{label}: {_fmt(current) if current is not None else '?'} "
                        f"→ {_fmt(value)}"),
        }
        if key not in TUNABLE_KEYS:
            item["hard_limit"] = True
            item["note"] = "🔒 보호된 키 — 변경 불가 (§4 하드리밋/§13 화이트리스트 밖)"
        elif _same(current, value):
            item["note"] = "이미 같은 값이에요"
        elif live:
            item["locked"] = True
            item["note"] = "🔒 실전 모드 — 모의에서만 적용"
        else:
            item["applicable"] = True
        items.append(item)

    items.sort(key=lambda i: (not i["applicable"], i["label"]))

    # 미반영 고급 규칙(R/R·VWAP·OBV·볼린저 수축·재료 강도 등) — "도입 가능" 항목.
    # 파라미터화돼 있지 않아 yaml 로 못 바꾸므로 "도입 예정(로드맵)"으로만 등록한다.
    pending_items = [
        {"label": p.label, "sample": p.sample, "kind": "pending"}
        for p in pending
    ]

    n_ok = sum(1 for i in items if i["applicable"])
    return {
        "ok": True, "synced": True, "mode": mode,
        "title": data.get("title", ""),
        "count": len(items), "applicable": n_ok,
        "items": items, "pending": pending_items,
        "message": (
            f"노션에서 전략 항목 {len(items)}개(적용 가능 {n_ok}개) + "
            f"도입 가능 {len(pending_items)}개를 찾았어요."
            if (items or pending_items) else
            "노션 내용에서 전략에 바로 반영할 수치를 못 찾았어요."
        ),
    }


# ─────────────────────────── 액션: apply ───────────────────────────

def action_apply(payload: dict, editor: StrategyEditor, mode: str,
                 ts: str, date: str) -> dict:
    items = payload.get("items") or []
    pend = payload.get("pending") or []
    if not items and not pend:
        return {"ok": False,
                "reason": "적용할 항목이 없어요. 체크박스로 항목을 선택해 주세요."}
    if mode != "paper":
        return {"ok": False, "locked": True, "applied": [], "failed": [],
                "pending_registered": [],
                "reason": "🔒 실전 모드에서는 전략 수정 불가 (§3.3)"}

    history = _load_applied()
    applied, failed = [], []
    for it in items:
        key = str(it.get("key") or "").strip()
        if not key:
            continue
        label = str(it.get("label") or KEY_LABELS.get(key, key))
        value = coerce_value(it.get("value", it.get("proposed")))
        reason = str(it.get("reason") or "노션 학습 반영")
        res = editor.apply(
            key, value, ts=ts, date=date, source="notion",
            reason=f"노션: {reason}", label=label,
        )
        d = res.to_dict()
        agent_id, agent_label = _agent_for(key)
        d["label"] = label
        d["agent"] = agent_id
        d["agent_label"] = agent_label
        if res.ok:
            applied.append(d)
            history.append({
                "ts": ts, "date": date, "kind": "param", "source": "notion",
                "key": res.key, "label": label, "agent": agent_id,
                "from": res.before, "to": res.after, "display": res.display,
                "reason": reason, "commit": res.commit,
                "improvement_id": res.improvement_id,
            })
        else:
            failed.append(d)

    # 미반영 고급 규칙 → 도입 예정(로드맵) 등록. 실제 yaml 변경 없음(코드 구현 필요).
    pending_registered: list[dict] = []
    for p in pend:
        label = str(p.get("label") or "").strip()
        if not label:
            continue
        rec = {
            "ts": ts, "date": date, "kind": "pending", "source": "notion",
            "label": label, "sample": str(p.get("sample") or ""),
            "status": "roadmap",
            "note": "코드 구현이 필요한 고급 규칙 — 도입 예정으로 등록했어요(파라미터 미변경).",
        }
        history.append(rec)
        pending_registered.append(rec)

    _save_applied(history)

    msgs = []
    if applied:
        msgs.append("✅ 전략 " + str(len(applied)) + "건 반영 — "
                    + " · ".join(a["display"] for a in applied))
    if pending_registered:
        msgs.append("📌 도입 예정 " + str(len(pending_registered)) + "건 등록")
    if failed:
        msgs.append("❌ " + str(len(failed)) + "건 실패")
    return {
        "ok": bool(applied or pending_registered), "mode": mode,
        "applied": applied, "failed": failed,
        "pending_registered": pending_registered,
        "message": " · ".join(msgs) if msgs else "변경을 적용하지 못했어요.",
    }


# ─────────────────────────── 액션: history ───────────────────────────

def action_history() -> dict:
    history = _load_applied()
    # 최신순.
    timeline = list(reversed(history))
    params = [h for h in timeline if h.get("kind") == "param"]
    pending = [h for h in timeline if h.get("kind") == "pending"]
    return {
        "ok": True, "total": len(history),
        "decisions": timeline[:30],
        "param_count": len(params), "pending_count": len(pending),
    }


# ─────────────────────────── 진입점 ───────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="노션 학습 추출·적용·이력")
    parser.add_argument("--action", required=True,
                        choices=["extract", "apply", "history"])
    args = parser.parse_args()

    now = datetime.now(KST)
    ts, date = now.isoformat(), now.strftime("%Y%m%d")

    try:
        cfg = KisClientConfig.from_files(project_root=ROOT)
        mode = cfg.mode.value
    except Exception as exc:  # noqa: BLE001
        return _emit({"ok": False, "reason": f"설정 로드 실패: {exc}"[:200]})

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
    if args.action == "extract":
        return _guard(lambda: action_extract(mode))

    raw = _read_stdin()
    payload: dict = {}
    if raw:
        try:
            payload = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            return _emit({"ok": False,
                          "reason": f"파싱 오류: 입력 JSON 형식이 아니에요 — {exc}"[:200]})
    if not isinstance(payload, dict):
        return _emit({"ok": False, "reason": "입력은 JSON object 여야 해요."})

    editor = StrategyEditor(
        config_path=CONFIG_PATH, memory_dir=MEMORY_DIR, project_root=ROOT, mode=mode,
    )
    if args.action == "apply":
        return _guard(lambda: action_apply(payload, editor, mode, ts, date))
    return _emit({"ok": False, "reason": "알 수 없는 액션"})


if __name__ == "__main__":
    raise SystemExit(main())
