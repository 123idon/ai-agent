"""Token domain.

토큰 발급/캐시는 traidair가 자체 보관한다(29분 캐시).
ai-agent는 토큰을 보유하지 않으며 401 응답을 받으면 KisClient가
/api/kis/token을 호출해 traidair의 캐시 재발급만 트리거한다.
"""
from .models import TokenSlice

__all__ = ["TokenSlice"]
