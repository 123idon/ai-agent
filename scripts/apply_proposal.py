"""단일 메타 제안 적용 + 검수 + 자동 커밋 (CLAUDE.md §2.7, §3.3, §11).

traidair HTS의 제안 카드 **✅ 적용** 버튼이 호출한다. ``state/evolve_result.json``에서
``proposal_id``로 제안을 찾아 ``config/strategy_params.yaml``에 반영하고, 다음 3단계로
**검수**한 뒤 결과 JSON을 stdout 한 줄로 출력한다(traidair가 파싱).

  a. 파일 수정      : ``apply_proposal_to_file`` (주석 보존·화이트리스트 스칼라만, §13)
  b. 재읽기 검증    : 수정 후 파일을 다시 yaml 파싱해 값이 실제로 바뀌었는지 확인
  c. GitHub 자동커밋 : 검증 성공 시 ``git add`` + ``git commit``

규칙:
- **paper 모드에서만 적용**한다(§3.3 잠금). live면 ``{ok:false, locked:true}`` 반환.
- 화이트리스트(``TUNABLE_KEYS``) 밖 키·하드리밋은 거부한다(§4, §13.1).
- 실패 시 ``reason``에 **왜 안 됐는지** 구체적으로 담는다.

usage: ``python scripts/apply_proposal.py <proposal_id>``  (또는 env ``APPLY_PROPOSAL_ID``)
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

# Windows 콘솔/파이프 기본 인코딩(cp949)에서 이모지(✅ 등) 출력 시 UnicodeEncodeError가
# 나므로 stdout/stderr를 UTF-8로 강제한다(Node가 stdout JSON을 UTF-8로 파싱).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

from agents.meta.optimizer.main import (  # noqa: E402
    TUNABLE_KEYS,
    OptimizationProposal,
    ParamChange,
    apply_proposal_to_file,
)
from core.kis_client import KisClientConfig, Mode  # noqa: E402


def _emit(data: dict) -> int:
    """결과 JSON을 stdout 한 줄로 출력. 항상 0 반환(에러도 JSON으로 전달)."""
    sys.stdout.write(json.dumps(data, ensure_ascii=False))
    sys.stdout.flush()
    return 0


def _yaml_value(config_path: Path, dotted_key: str):
    import yaml
    doc = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    node = doc
    for seg in dotted_key.split("."):
        if not isinstance(node, dict) or seg not in node:
            return None
        node = node[seg]
    return node


def _same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
    return a == b


def _git_commit(root: Path, rel_path: str, message: str) -> dict:
    """변경 파일을 add + commit. (push는 하지 않음 — 로컬 커밋만.)"""
    try:
        subprocess.run(
            ["git", "-C", str(root), "add", rel_path],
            check=True, capture_output=True, text=True, timeout=20,
        )
        # 변경이 스테이징되지 않았으면(이미 동일 값) 커밋 생략.
        diff = subprocess.run(
            ["git", "-C", str(root), "diff", "--cached", "--quiet", "--", rel_path],
            capture_output=True, text=True, timeout=20,
        )
        if diff.returncode == 0:
            return {"committed": False, "reason": "변경 내용이 없어 커밋 생략"}
        proc = subprocess.run(
            ["git", "-C", str(root), "commit", "-m", message, "--", rel_path],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return {"committed": False,
                    "reason": (proc.stderr or proc.stdout or "git commit 실패").strip()[:200]}
        rev = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=20,
        )
        return {"committed": True, "commit": rev.stdout.strip()}
    except FileNotFoundError:
        return {"committed": False, "reason": "git 미설치 — 커밋 생략(파일은 정상 적용됨)"}
    except subprocess.TimeoutExpired:
        return {"committed": False, "reason": "git 응답 지연 — 커밋 생략(파일은 정상 적용됨)"}
    except Exception as exc:  # noqa: BLE001
        return {"committed": False, "reason": f"git 오류: {exc}"[:200]}


def main() -> int:
    pid = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("APPLY_PROPOSAL_ID") or "").strip()
    if not pid:
        return _emit({"ok": False, "reason": "proposal_id가 비어 있어요."})

    root = Path(__file__).parents[1]
    config_path = root / "config" / "strategy_params.yaml"
    result_path = root / "state" / "evolve_result.json"

    # ── 모드 게이트 (§3.3) ──
    try:
        cfg = KisClientConfig.from_files(project_root=root)
        mode = cfg.mode
    except Exception as exc:  # noqa: BLE001
        return _emit({"ok": False, "reason": f"설정 로드 실패: {exc}"[:200]})
    if mode != Mode.PAPER:
        return _emit({
            "ok": False, "locked": True,
            "reason": "🔒 실전 모드에서는 전략 수정 불가 (파라미터 잠금, §3.3)",
        })

    # ── 제안 조회 ──
    if not result_path.exists():
        return _emit({"ok": False, "reason": "진화 결과가 없어요 — 먼저 🧬 진화+를 실행하세요."})
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return _emit({"ok": False, "reason": f"진화 결과 파싱 실패: {exc}"[:200]})

    proposal = next((p for p in (data.get("proposals") or []) if p.get("id") == pid), None)
    if proposal is None:
        return _emit({"ok": False, "reason": "제안을 찾을 수 없어요 (진화+를 다시 실행해 보세요)."})

    raw_changes = proposal.get("changes") or []
    if not raw_changes:
        return _emit({
            "ok": False,
            "reason": "이 제안에는 적용할 전략 변경이 없어요 (권고/관찰만).",
        })

    # 화이트리스트 사전 점검 — 거부 사유를 구체적으로.
    bad = [c.get("key") for c in raw_changes if c.get("key") not in TUNABLE_KEYS]
    if bad:
        return _emit({
            "ok": False,
            "reason": f"보호된 키라 수정할 수 없어요: {', '.join(map(str, bad))} "
                      f"(하드리밋/구조 값은 변경 금지, §4·§13).",
        })

    # 적용 전 현재값 스냅샷(검증·메시지용).
    pre = {c["key"]: _yaml_value(config_path, c["key"]) for c in raw_changes}

    changes = tuple(
        ParamChange(
            key=c["key"], from_value=pre.get(c["key"]),
            to_value=c.get("to"), reason=c.get("reason", ""),
        )
        for c in raw_changes
    )
    proposal_obj = OptimizationProposal(
        proposal_id=pid, kind=proposal.get("kind", "strategy"),
        mode=mode.value, created_ts="", rationale=proposal.get("rationale", ""),
        changes=changes,
    )

    # ── a. 파일 수정 ──
    try:
        applied = apply_proposal_to_file(proposal_obj, config_path)
    except Exception as exc:  # noqa: BLE001
        return _emit({"ok": False, "reason": f"❌ 적용 실패: 파일 쓰기 오류 — {exc}"[:200]})
    if not applied:
        return _emit({
            "ok": False,
            "reason": "❌ 적용 실패: 파일에서 해당 값을 찾지 못했어요(스칼라 교체 실패).",
        })

    # ── b. 재읽기 검증 ──
    verified: list[dict] = []
    failed: list[str] = []
    for ch in applied:
        post = _yaml_value(config_path, ch.key)
        if _same(post, ch.to_value):
            verified.append({
                "key": ch.key, "from": ch.from_value, "to": ch.to_value,
                "reason": ch.reason,
            })
        else:
            failed.append(f"{ch.key}(기대 {ch.to_value}, 실제 {post})")
    if failed:
        return _emit({
            "ok": False,
            "reason": "❌ 적용 실패: 파일에 값이 반영되지 않았어요 — " + ", ".join(failed),
        })

    # ── c. GitHub 자동 커밋 ──
    summary = ", ".join(f"{v['key']} {v['from']}→{v['to']}" for v in verified)
    git = _git_commit(
        root, "config/strategy_params.yaml",
        f"chore(strategy): 메타 제안 {pid} 적용 — {summary}",
    )

    return _emit({
        "ok": True,
        "applied": verified,
        "committed": git.get("committed", False),
        "commit": git.get("commit"),
        "gitReason": git.get("reason"),
        "message": f"✅ 적용 완료 ({summary})",
    })


if __name__ == "__main__":
    sys.exit(main())
