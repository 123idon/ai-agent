"""Standard inter-agent message envelope (CLAUDE.md §6.2).

In-process Bus는 dataclass payload를 직접 전달하지만,
journal/외부 시스템 영속화 시점에 본 envelope으로 wrapping 한다.
redis-streams 등으로 교체할 때 본 모듈만 재사용한다.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

KST = timezone(timedelta(hours=9))

T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    """표준 메시지 봉투 (CLAUDE.md §6.2)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    msg_id: str
    topic: str
    ts: datetime
    mode: str       # "paper" | "live"
    sender: str
    payload: T
    trace_id: str


def serialize(obj: Any) -> Any:
    """dataclass / Pydantic / Enum / datetime / list / dict 를 JSON 호환 형태로 변환."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: serialize(v) for k, v in asdict(obj).items()}
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [serialize(v) for v in obj]
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def wrap(
    topic: str,
    payload: Any,
    *,
    sender: str,
    mode: str,
    ts: datetime | None = None,
    trace_id: str | None = None,
    msg_id: str | None = None,
) -> dict[str, Any]:
    """JSON-ready envelope dict 생성."""
    return {
        "msg_id": msg_id or str(uuid4()),
        "topic": topic,
        "ts": (ts or datetime.now(KST)).isoformat(),
        "mode": mode,
        "sender": sender,
        "payload": serialize(payload),
        "trace_id": trace_id or str(uuid4()),
    }


def parse(envelope_dict: dict[str, Any]) -> Envelope[dict[str, Any]]:
    """dict → Envelope[dict] 검증 파싱."""
    return Envelope[dict[str, Any]].model_validate(envelope_dict)
