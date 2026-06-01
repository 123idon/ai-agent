"""백테스트 단일 실행 락 회귀 테스트 (이중 실행 → "며칠 만에 끝남" 근본 원인 가드).

두 엔진이 동시에 기동해 같은 state/ 제어판을 두고 싸우면 한쪽의 정지/정리가 다른 쪽을
조기 종료시킨다. ``_acquire_singleton_lock`` 은 ``O_CREAT|O_EXCL`` 원자적 생성으로
**정확히 하나만** 락을 잡게 보장한다. 이 테스트가 그 불변식을 고정한다.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

rb = importlib.import_module("scripts.run_backtest")


@pytest.fixture
def lock_in_tmp(tmp_path, monkeypatch):
    lp = tmp_path / "backtest.lock"
    monkeypatch.setattr(rb, "_lock_path", lambda: lp)
    return lp


def test_acquire_then_reentrant_holds(lock_in_tmp):
    # 처음 획득 성공 → 같은 프로세스(우리 pid)는 재진입해도 보유로 간주.
    assert rb._acquire_singleton_lock() is True
    assert lock_in_tmp.exists()
    assert rb._acquire_singleton_lock() is True   # 재진입(우리 자신)


def test_second_engine_yields_while_lock_fresh(lock_in_tmp):
    # 다른 엔진(다른 pid)이 신선한 락을 보유 중이면 → 새 기동은 양보(False).
    lock_in_tmp.write_text(
        json.dumps({"pid": os.getpid() + 12345, "ts": time.time()}), encoding="utf-8",
    )
    # mtime 도 방금이라 신선.
    assert rb._acquire_singleton_lock() is False


def test_stale_lock_is_reclaimed(lock_in_tmp, monkeypatch):
    # 죽은 엔진 잔재(오래된 mtime)는 회수되어 새 엔진이 획득.
    lock_in_tmp.write_text(
        json.dumps({"pid": os.getpid() + 12345, "ts": 0.0}), encoding="utf-8",
    )
    old = time.time() - (rb._LOCK_TTL + 5)
    os.utime(lock_in_tmp, (old, old))
    assert rb._acquire_singleton_lock() is True
    # 회수 후 우리 pid 로 갱신됐는지.
    assert json.loads(lock_in_tmp.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_release_only_removes_own_lock(lock_in_tmp):
    # 다른 엔진의 락은 해제하지 않는다(불침).
    foreign = {"pid": os.getpid() + 999, "ts": time.time()}
    lock_in_tmp.write_text(json.dumps(foreign), encoding="utf-8")
    rb._release_singleton_lock()
    assert lock_in_tmp.exists()            # 남의 락은 그대로
    # 우리 락이면 해제.
    lock_in_tmp.write_text(rb._lock_payload().decode("utf-8"), encoding="utf-8")
    rb._release_singleton_lock()
    assert not lock_in_tmp.exists()


def test_only_one_of_two_concurrent_acquirers_wins(lock_in_tmp, monkeypatch):
    """동시 기동 시뮬레이션 — 한 번에 정확히 하나만 락을 잡는다(핵심 불변식).

    같은 프로세스에선 pid 가 같아 '재진입=보유'가 되므로, 두 번째 기동이 *다른 엔진*
    임을 흉내내기 위해 두 번째 호출 동안만 getpid 를 다른 값으로 패치한다.
    """
    assert rb._acquire_singleton_lock() is True       # 엔진 A 획득
    monkeypatch.setattr(os, "getpid", lambda: os.getppid() or 424242)
    assert rb._acquire_singleton_lock() is False      # 엔진 B 양보
