"""전체 팀 회의(💬 상담 → 🤝 전체 팀 회의) 결과 저장 + 자동 커밋 (CLAUDE.md §11, §16.9).

traidair HTS 💬 상담 탭의 전체 팀 회의가 끝나면, 회의 한 라운드 기록(질문·각 부서 발언·
토론·CEO 종합·추출된 개선안)을 하나의 레코드로 받아 ``data/memory/team_meeting_log.json`` 에
append-only 로 저장하고 GitHub 에 자동 커밋한다.

- 입력: 회의 레코드 JSON 한 건. ``--`` 인자로 받거나 stdin 으로 받는다.
- 저장: ``data/memory/team_meeting_log.json`` (배열, 최근 200건만 유지).
- 커밋: ``git add`` + ``git commit`` (실패해도 파일 저장은 유지, 비치명).
- 출력: 결과 JSON 한 줄(traidair 가 파싱). ``{ok, count, commit?, committed, ts}``.

이 스크립트는 전략 파라미터를 직접 바꾸지 않는다(§11 학습부 원칙). 개선안 적용은
``scripts/consult_apply.py`` 가 별도로 담당한다(✅ 적용 버튼, paper 전용).

usage: ``python scripts/save_meeting.py <record_json>`` 또는 ``... | python scripts/save_meeting.py``
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows 콘솔/파이프(cp949)에서 이모지 출력 시 UnicodeEncodeError 방지 — UTF-8 강제.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

KST = timezone(timedelta(hours=9))
MAX_RECORDS = 200


def _emit(data: dict) -> int:
    sys.stdout.write(json.dumps(data, ensure_ascii=False))
    sys.stdout.flush()
    return 0


def _git_commit(root: Path, rel_path: str, message: str) -> dict:
    enc = {"encoding": "utf-8", "errors": "replace"}
    try:
        subprocess.run(["git", "-C", str(root), "add", rel_path],
                       check=True, capture_output=True, text=True, timeout=20, **enc)
        diff = subprocess.run(["git", "-C", str(root), "diff", "--cached", "--quiet", "--", rel_path],
                              capture_output=True, text=True, timeout=20, **enc)
        if diff.returncode == 0:
            return {"committed": False, "reason": "변경 내용이 없어 커밋 생략"}
        proc = subprocess.run(["git", "-C", str(root), "commit", "-m", message, "--", rel_path],
                              capture_output=True, text=True, timeout=30, **enc)
        if proc.returncode != 0:
            return {"committed": False, "reason": (proc.stderr or proc.stdout or "git commit 실패").strip()[:200]}
        rev = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=20, **enc)
        return {"committed": True, "commit": rev.stdout.strip()}
    except FileNotFoundError:
        return {"committed": False, "reason": "git 미설치 — 커밋 생략(파일은 정상 저장됨)"}
    except subprocess.TimeoutExpired:
        return {"committed": False, "reason": "git 응답 지연 — 커밋 생략(파일은 정상 저장됨)"}
    except Exception as exc:  # noqa: BLE001
        return {"committed": False, "reason": f"git 오류: {exc}"[:200]}


def main() -> int:
    raw = ""
    if len(sys.argv) >= 2 and sys.argv[1].strip():
        raw = sys.argv[1]
    else:
        # stdin 을 **바이너리로 읽어 UTF-8 로 직접 디코드** — Windows 콘솔/파이프(cp949)나
        # PYTHONIOENCODING 미설정 환경에서도 한글 JSON 이 surrogate 로 깨지지 않게 한다.
        try:
            raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            raw = sys.stdin.read()
    raw = (raw or "").strip()
    if not raw:
        return _emit({"ok": False, "reason": "회의 레코드가 비었어요."})
    try:
        record = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return _emit({"ok": False, "reason": f"레코드 JSON 파싱 실패: {exc}"[:200]})
    if not isinstance(record, dict):
        return _emit({"ok": False, "reason": "레코드는 객체(JSON object)여야 해요."})

    root = Path(__file__).parents[1]
    mem_dir = root / "data" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    log_path = mem_dir / "team_meeting_log.json"

    # 서버가 ts 를 안 넣었으면 KST 로 채운다.
    record.setdefault("ts", datetime.now(KST).isoformat(timespec="seconds"))

    # ── 기존 로그 로드(깨졌으면 빈 배열로 복구) ──
    existing: list = []
    if log_path.exists():
        try:
            loaded = json.loads(log_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = loaded
        except Exception:  # noqa: BLE001
            existing = []

    existing.append(record)
    if len(existing) > MAX_RECORDS:
        existing = existing[-MAX_RECORDS:]

    try:
        log_path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        return _emit({"ok": False, "reason": f"파일 저장 실패: {exc}"[:200]})

    # ── GitHub 자동 커밋 ──
    n_imp = len(record.get("improvements") or [])
    q = str(record.get("q") or "정기 회의")[:40]
    git = _git_commit(root, "data/memory/team_meeting_log.json",
                      f"chore(meeting): 전체 팀 회의 기록 — {q} (개선안 {n_imp}건)")

    out = {"ok": True, "count": len(existing), "ts": record["ts"]}
    out.update(git)
    return _emit(out)


if __name__ == "__main__":
    raise SystemExit(main())
