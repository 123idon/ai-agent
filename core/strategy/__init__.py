"""전략 파라미터 적용 계층 (CLAUDE.md §2.7, §3.3, §11, §13).

상담·복기(학습)·노션에서 나온 모든 ``strategy_params.yaml`` 변경은 ``StrategyEditor``
단일 진입점을 거친다: 화이트리스트 검증 → 주석 보존 leaf 교체 → 재읽기 검증 →
git 자동 커밋 → ``ImprovementLog`` 영구 기록. 하드리밋(§4)은 절대 손대지 않는다.
"""
from .editor import StrategyApplyResult, StrategyEditor

__all__ = ["StrategyEditor", "StrategyApplyResult"]
