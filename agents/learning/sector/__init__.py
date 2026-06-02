"""학습부 — 섹터 데이터 추출 + 섹터 강도 가산점 (CLAUDE.md §2.2.1).

전일 종가 기준 섹터 등락률/대장주를 산출(``SectorDataProvider.sector_data``)하고,
스크리닝이 종목별 가산점(``SectorSnapshot.bonus_for``)을 점수에 합산한다.
"""
from .classifier import SectorClassifier
from .main import SectorDataProvider, SectorInfo, SectorSnapshot

__all__ = [
    "SectorClassifier",
    "SectorDataProvider",
    "SectorInfo",
    "SectorSnapshot",
]
