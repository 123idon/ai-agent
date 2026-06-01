"""상담(💬) 자연어 → 전략 파라미터 변경 추출 (CLAUDE.md §11, §13).

운영자/에이전트가 상담에서 "RSI 기준을 55~65로 좁히자", "타임스톱 25분으로" 같은
문장을 말하면, 규칙 기반(LLM 미사용 §15.4·§21)으로 ``strategy_params.yaml`` 의
화이트리스트 키 변경으로 변환한다. 노션 규칙 추출(§23)도 같은 파서를 재사용한다.
"""
from .parser import Suggestion, extract_changes

__all__ = ["Suggestion", "extract_changes"]
