"""상담(💬) → 전략 즉시 반영 + 영구 기억 (CLAUDE.md §2.7, §3.3, §11, §13).

traidair HTS 💬 상담 탭에서 운영자/에이전트가 개선사항을 말하면 **실제 파일에 반영**한다.
두 가지 호출 방식:

  1. 직접 적용 : ``consult_apply.py <dotted_key> <value>``
  2. 자연어   : ``consult_apply.py --text "RSI 기준을 55~65로 좁히자"``
                → 규칙 기반 파서가 화이트리스트 키 변경을 추출해 일괄 적용
  3. 자연어(stdin) : ``echo '{"text":"신호 조건 4개로"}' | consult_apply.py --stdin``
                → 긴 한글 문장을 argv 인코딩 깨짐 없이 stdin(UTF-8)으로 전달(Windows 안전).
                  raw 텍스트 또는 ``{"text": ...}`` JSON 둘 다 허용. traidair 서버가 사용.

모든 적용은 ``StrategyEditor`` 단일 진입점을 거친다(화이트리스트 → 주석 보존 leaf 교체
→ 재읽기 검증 → git 자동 커밋 → ``ImprovementLog`` 영구 기록). 상담 발화·결과는
``ConsultLog`` 에 누적되어 다음 상담이 맥락을 이어받는다(세션 간 기억 초기화 해결).

규칙:
- **paper 모드에서만 적용**(§3.3 잠금). live면 ``{ok:false, locked:true}``.
- 변경 전후 값을 ``applied[].display`` 로 돌려준다("RSI 기준: [50, 65] → [55, 65]로 변경됨").
- 자연어에서 아무 변경도 못 뽑으면 ``{ok:false, warning}`` — "상담만 하고 안 고치면 의미 없음" 경고.
- 결과 JSON을 stdout 한 줄로 출력(traidair 파싱). stdout UTF-8 강제.

usage:
  python scripts/consult_apply.py <dotted_key> <value>
  python scripts/consult_apply.py --text "<상담 문장>"
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

from core.consult import detect_hard_limit_request, extract_changes  # noqa: E402
from core.kis_client import KisClientConfig, Mode  # noqa: E402
from core.memory import ConsultLog  # noqa: E402
from core.strategy import StrategyEditor  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parents[1]
MEMORY_DIR = ROOT / "data" / "memory"
CONFIG_PATH = ROOT / "config" / "strategy_params.yaml"


def _emit(data: dict) -> int:
    sys.stdout.write(json.dumps(data, ensure_ascii=False))
    sys.stdout.flush()
    return 0


def _coerce(raw: str):
    s = raw.strip()
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    # 인라인 리스트 "[55, 65]" / "55,65"
    if s.startswith("[") or ("," in s and all(
        _is_num(p) for p in s.strip("[]").split(",")
    )):
        return [_coerce(p) for p in s.strip("[]").split(",")]
    try:
        if "." not in s and "e" not in low:
            return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def _is_num(p: str) -> bool:
    try:
        float(p.strip())
        return True
    except ValueError:
        return False


def _read_stdin_text() -> str:
    """stdin(UTF-8)에서 상담 문장을 읽는다. raw 텍스트 또는 {"text": ...} JSON 허용.

    Windows에서 긴 한글을 argv로 넘기면 코드페이지에 따라 깨질 수 있어, traidair 서버는
    문장을 stdin 으로 전달한다(meeting_apply.py 와 동일 패턴).
    """
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        raw = (sys.stdin.read() or "").strip()
    if not raw:
        return ""
    if raw[0] in "{[":
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return str(obj.get("text", "")).strip()
        except Exception:  # noqa: BLE001
            pass
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description="상담 → 전략 반영")
    parser.add_argument("key", nargs="?", help="dotted key (직접 적용)")
    parser.add_argument("value", nargs="?", help="새 값 (직접 적용)")
    parser.add_argument("--text", default=None, help="상담 문장(자연어 파싱)")
    parser.add_argument("--stdin", action="store_true",
                        help="상담 문장을 stdin(UTF-8)에서 읽음 — 한글 argv 깨짐 방지")
    args = parser.parse_args()

    # stdin 모드: 긴 한글 문장을 안전하게 받아 --text 로 처리.
    if args.stdin and not args.text:
        args.text = _read_stdin_text()

    now = datetime.now(KST)
    ts, date = now.isoformat(), now.strftime("%Y%m%d")

    # ── 모드 게이트 (§3.3) ──
    try:
        cfg = KisClientConfig.from_files(project_root=ROOT)
        mode = cfg.mode
    except Exception as exc:  # noqa: BLE001
        return _emit({"ok": False, "reason": f"설정 로드 실패: {exc}"[:200]})

    editor = StrategyEditor(
        config_path=CONFIG_PATH, memory_dir=MEMORY_DIR, project_root=ROOT,
        mode=mode.value,
    )
    consult = ConsultLog.load(MEMORY_DIR)

    # ── 자연어 모드 ──
    if args.text:
        consult.add_turn(ts=ts, role="operator", text=args.text)
        suggestions = extract_changes(args.text)
        if not suggestions:
            # 하드리밋(§4) 변경 요청이면 "왜 안 되는지" 명확히(요구 5: 거부할 때만 이유 명시).
            hard = detect_hard_limit_request(args.text)
            if hard:
                return _emit({
                    "ok": False, "locked": True, "hard_limit": True,
                    "reason": "🔒 " + hard, "applied": [],
                })
            return _emit({
                "ok": False,
                "warning": "⚠️ 상담 내용에서 바꿀 전략 항목을 못 찾았어요. "
                           "상담만 하고 실제로 안 고치면 의미가 없어요 — "
                           "예: '신호 조건 4개로', 'RSI 기준 55~65로', '하드 손절 -3%로'처럼 "
                           "수치를 말해 주세요.",
                "applied": [],
            })
        applied, failed = [], []
        for s in suggestions:
            res = editor.apply(
                s.key, s.value, ts=ts, date=date, source="consult",
                reason=f"상담: {s.reason}", label=s.label,
            )
            (applied if res.ok else failed).append(res.to_dict())
        if applied:
            consult.add_turn(
                ts=now.isoformat(), role="agent",
                text="상담 내용을 전략에 반영했어요.",
                applied=[{"key": a["key"], "from": a["from"], "to": a["to"]} for a in applied],
            )
        if mode != Mode.PAPER and not applied:
            return _emit({"ok": False, "locked": True,
                          "reason": "🔒 실전 모드에서는 전략 수정 불가 (§3.3)",
                          "applied": [], "failed": failed})
        return _emit({
            "ok": bool(applied), "applied": applied, "failed": failed,
            "message": ("✅ " + " · ".join(a["display"] for a in applied))
            if applied else "변경을 적용하지 못했어요.",
        })

    # ── 직접 적용 모드 ──
    if not args.key or args.value is None:
        return _emit({"ok": False, "reason": "사용법: consult_apply.py <key> <value>  또는  --text \"문장\""})
    value = _coerce(args.value)
    res = editor.apply(
        args.key, value, ts=ts, date=date, source="consult", reason="상담 직접 적용",
    )
    if res.ok:
        consult.add_turn(
            ts=now.isoformat(), role="operator", text=f"{args.key} = {value}",
            applied=[{"key": res.key, "from": res.before, "to": res.after}],
        )
    out = res.to_dict()
    # traidair ✅ 적용 버튼 하위호환: applied 배열 유지.
    out["applied"] = (
        [{"key": res.key, "from": res.before, "to": res.after, "display": res.display}]
        if res.ok else []
    )
    out["message"] = ("✅ " + res.display) if res.ok else res.reason
    return _emit(out)


if __name__ == "__main__":
    sys.exit(main())
