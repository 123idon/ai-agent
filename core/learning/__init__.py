"""복기 → 학습 → 반영 자동 파이프라인 (CLAUDE.md §2.6, §11).

매매 저널을 집계해 반복되는 손실 패턴에서 전략 개선안을 도출한다. 메타부
``OptimizerAgent`` 의 제안과 함께 ``StrategyEditor`` 로 적용된다(paper 전용).
"""
from .review import ReviewSuggestion, ReviewLearner

__all__ = ["ReviewLearner", "ReviewSuggestion"]
