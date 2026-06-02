"""학습부 노션 동기화 CLI (CLAUDE.md §2.6, §11).

Notion 페이지를 읽어 5개 부서 카테고리로 분류 → ``data/memory/notion_knowledge.json``
저장. 변경 시 ``data/memory/notion_updates.log`` 기록.

사용법::

    python scripts/sync_notion.py                # 변경 감지 동기화
    python scripts/sync_notion.py --force        # 변경 없어도 재기록
    python scripts/sync_notion.py --status       # 현황 출력
    python scripts/sync_notion.py --json         # 기계용 1줄 JSON(traidair 상담 탭)
    python scripts/sync_notion.py --page <id>    # 페이지 ID 지정
    python scripts/sync_notion.py --install-schedule   # 매일 16:10 작업 등록(Windows)

매일 1회 자동 확인(요구 5)은 Windows 작업 스케줄러 ``ai-team-notion-sync`` 로 등록한다
(§18 캔들 수집 ``ai-team-candle-collect`` 15:40 다음, 16:10). ``--install-schedule`` 참고.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

# Windows 콘솔/파이프(cp949)에서 이모지/한글 출력 시 UnicodeEncodeError 방지.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

from datetime import datetime, timedelta, timezone  # noqa: E402

from agents.learning.notion_sync import NotionSyncAgent  # noqa: E402
from core.kis_client import KisClientConfig  # noqa: E402
from core.notion_client import NotionAuthError, NotionConfig  # noqa: E402
from core.strategy import StrategyEditor  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parents[1]
MEMORY_DIR = ROOT / "data" / "memory"
CONFIG_PATH = ROOT / "config" / "strategy_params.yaml"
TASK_NAME = "ai-team-notion-sync"
WATCH_TASK_NAME = "ai-team-notion-watch"


def _apply_to_strategy(agent: NotionSyncAgent) -> dict:
    """동기화된 노션 지식을 strategy_params.yaml 에 우선 반영(§23 요구 1·3)."""
    now = datetime.now(KST)
    try:
        mode = KisClientConfig.from_files(project_root=ROOT).mode
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"설정 로드 실패: {exc}"[:200]}
    editor = StrategyEditor(
        config_path=CONFIG_PATH, memory_dir=MEMORY_DIR, project_root=ROOT,
        mode=mode.value,
    )
    return agent.apply_to_strategy(editor, ts=now.isoformat(), date=now.strftime("%Y%m%d"))


def _install_schedule() -> int:
    """매일 16:10 ``sync_notion.py`` 를 실행하는 Windows 작업 등록."""
    python = sys.executable or "python"
    script = str(ROOT / "scripts" / "sync_notion.py")
    cmd = [
        "schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", "16:10",
        "/TN", TASK_NAME,
        "/TR", f'"{python}" "{script}"',
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print("schtasks 미존재(비 Windows?) — 스케줄 등록 생략")
        return 1
    except subprocess.TimeoutExpired:
        print("schtasks 응답 지연 — 스케줄 등록 생략")
        return 1
    if proc.returncode == 0:
        print(f"✅ 작업 등록: {TASK_NAME} (매일 16:10)")
        return 0
    print(f"❌ 작업 등록 실패: {(proc.stderr or proc.stdout).strip()[:200]}")
    return 1


def _install_watch() -> int:
    """5분마다 노션 변경을 감지·반영하는 Windows 작업 등록(§23 요구 2)."""
    python = sys.executable or "python"
    script = str(ROOT / "scripts" / "sync_notion.py")
    cmd = [
        "schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", "5",
        "/TN", WATCH_TASK_NAME,
        "/TR", f'"{python}" "{script}" --apply --json',
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print("schtasks 미존재(비 Windows?) — 스케줄 등록 생략")
        return 1
    except subprocess.TimeoutExpired:
        print("schtasks 응답 지연 — 스케줄 등록 생략")
        return 1
    if proc.returncode == 0:
        print(f"✅ 작업 등록: {WATCH_TASK_NAME} (5분마다 변경 감지+반영)")
        return 0
    print(f"❌ 작업 등록 실패: {(proc.stderr or proc.stdout).strip()[:200]}")
    return 1


def _build_agent(page: str | None) -> NotionSyncAgent:
    cfg = NotionConfig.from_files(project_root=ROOT)
    if page:
        cfg = replace(cfg, page_id=page)
    return NotionSyncAgent(cfg, memory_dir=MEMORY_DIR)


async def _run(args: argparse.Namespace) -> int:
    try:
        agent = _build_agent(args.page)
    except NotionAuthError as exc:
        # --status 는 로컬 파일만 읽으므로 토큰 없이도 동작해야 한다.
        if args.status:
            agent = NotionSyncAgent(
                NotionConfig(token="-", page_id=args.page or ""), memory_dir=MEMORY_DIR,
            )
        else:
            out = {"ok": False, "error": str(exc)}
            print(json.dumps(out, ensure_ascii=False) if args.json else f"❌ {exc}")
            return 2

    if args.status:
        st = agent.status()
        if args.json:
            print(json.dumps(st, ensure_ascii=False))
        else:
            _print_status(st)
        return 0

    result = await agent.sync(force=args.force)

    # §23 요구 1·3: 동기화 후 전략 자동 반영(--apply). 변경 감지 시(또는 --force) 반영.
    if args.apply and result.get("ok"):
        if result.get("changed") or args.force:
            result["strategy"] = _apply_to_strategy(agent)

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    if not result.get("ok"):
        print(f"❌ 동기화 실패: {result.get('error')}")
        return 1
    s = result.get("stats", {})
    if result.get("changed"):
        print(f"✅ 노션 반영 완료: '{result.get('title')}' "
              f"— 총 {s.get('total_rules', 0)}건")
        print(f"   스크리닝 {s.get('screening',0)} · 신호 {s.get('signal',0)} · "
              f"리스크 {s.get('risk',0)} · 시장 {s.get('market_watch',0)} · "
              f"CEO {s.get('ceo',0)}")
    else:
        print(f"ℹ️  변경 없음 (마지막 확인 {result.get('fetched_at')})")
    strat = result.get("strategy")
    if strat and strat.get("ok"):
        ap = strat.get("applied", [])
        if ap:
            print(f"🔧 전략 반영 {len(ap)}건:")
            for a in ap:
                print(f"   - {a.get('display', a.get('key'))}")
        if strat.get("conflicts"):
            print(f"⚠️  전략 충돌 {len(strat['conflicts'])}건 — 노션 우선 적용됨")
        if strat.get("pending"):
            labels = ", ".join(p["label"] for p in strat["pending"])
            print(f"🕓 미반영(도입 예정): {labels}")
    return 0


def _print_status(st: dict) -> None:
    if not st.get("synced"):
        print(st.get("message", "미동기화"))
        return
    print(f"📚 노션 학습 현황: '{st.get('title')}'")
    print(f"   마지막 반영: {st.get('last_update')}  /  마지막 확인: {st.get('last_checked')}")
    print(f"   총 규칙 {st.get('total_rules', 0)}건")
    for key, a in (st.get("agents") or {}).items():
        print(f"   - {a.get('label','')} ({key}): {a.get('count',0)}건")
    if st.get("updates"):
        print("   최근 변경:")
        for line in st["updates"][-5:]:
            print(f"     {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description="노션 지식 동기화")
    parser.add_argument("--force", action="store_true", help="변경 없어도 재기록")
    parser.add_argument("--status", action="store_true", help="현황 출력")
    parser.add_argument("--json", action="store_true", help="기계용 JSON 출력")
    parser.add_argument("--page", default=None, help="노션 페이지 ID")
    parser.add_argument("--apply", action="store_true",
                        help="동기화 후 노션 규칙을 strategy_params.yaml 에 우선 반영(§23)")
    parser.add_argument("--install-schedule", action="store_true",
                        help="매일 16:10 자동 동기화 작업 등록(Windows)")
    parser.add_argument("--install-watch", action="store_true",
                        help="5분마다 노션 변경 감지+반영 작업 등록(Windows)")
    args = parser.parse_args()

    if args.install_schedule:
        return _install_schedule()
    if args.install_watch:
        return _install_watch()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
