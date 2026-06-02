"""랜덤 과거 날짜 백테스트 리플레이 엔진 (CLAUDE.md §17).

paper(모의) 모드를 실시간 시세 수신 대신 **랜덤한 과거 거래일을 그날인 것처럼
재생**하는 방식으로 운영한다. 핵심은 두 가지다:

- ``SimClock`` (core.time_utils): 전 에이전트가 참조하는 가상 시각.
- ``ReplayKisClient``: KisClient와 동일한 인터페이스를 제공하되, 모든 시세/지표를
  **가상 시각 이전(<=)으로만** 잘라서 반환해 룩어헤드 바이어스를 제거한다.

``BacktestRunner``가 랜덤 영업일을 골라 09:00→15:30을 분 단위로 전진시키며 기존
에이전트(스크리닝·신호·리스크·실행·포지션매니저·시장상황)를 그대로 구동한다.
"""
from .dashboard import BacktestDashboard
from .replay_client import ReplayKisClient
from .runner import BacktestRunner, DayResult

__all__ = ["ReplayKisClient", "BacktestRunner", "DayResult", "BacktestDashboard"]
