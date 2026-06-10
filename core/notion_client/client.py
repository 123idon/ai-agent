"""Notion REST API 비동기 클라이언트.

traidair `/api/notion-lecture`(server.js)와 동일한 방식으로 ``api.notion.com``의
블록을 재귀 수집한다. 차이점:
- ai-agent 측은 traidair를 거치지 않고 **Notion에 직접** 호출한다(토큰은
  ``config/kis_api.yaml``의 ``notion.token`` 또는 ``NOTION_TOKEN`` env).
- 자식 페이지(``child_page``)까지 재귀해 커리큘럼 하위 Phase 전체를 읽는다.
- 헤딩/자식페이지 단위로 ``NotionSection``으로 묶어 분류기가 쓰기 쉽게 만든다.

실패도 예외로 분리한다(``NotionAuthError``/``NotionError``). HTTP는 Notion이
4xx/5xx로 응답하므로 상태코드로 판정한다(KIS traidair의 ok 통일과 다름).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

NOTION_API = "https://api.notion.com"
NOTION_VERSION = "2022-06-28"
DEFAULT_PAGE_ID = "35a0717882e381ce8fc3d257a5c24e4b"  # 단타 트레이딩 마스터 커리큘럼


class NotionError(Exception):
    """Notion 호출 실패(네트워크/4xx/5xx)."""


class NotionAuthError(NotionError):
    """토큰 누락 또는 401/403."""


def _is_placeholder(value: object) -> bool:
    if not value:
        return True
    s = str(value).strip()
    if len(s) < 10:
        return True
    return "TODO" in s or "여기" in s or "your" in s.lower() or "xxxx" in s.lower()


def _to_uuid(page_id: str) -> str:
    """페이지 ID/URL → 8-4-4-4-12 UUID.

    허용 입력: 하이픈 없는 32자 hex, 하이픈 포함 36자 UUID, 전체 Notion URL
    (예: ``https://www.notion.so/제목-35a0...4e4b?pvs=4``). URL/슬러그면 끝의 32자
    hex(실제 ID)를 추출한다. 인식 실패 시 입력을 그대로(공백 제거) 반환한다.
    """
    s = (page_id or "").strip()
    if not s:
        return s
    # URL이면 쿼리/경로를 떼고 마지막 세그먼트만 사용
    if "/" in s:
        s = s.split("?", 1)[0].rstrip("/").split("/")[-1]
    # hex 이외 문자 제거 후 뒤쪽 32자(=실제 ID)를 취한다(제목 슬러그 대응)
    compact = re.sub(r"[^0-9a-fA-F]", "", s)
    if len(compact) >= 32:
        h = compact[-32:].lower()
        return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    return s


@dataclass(frozen=True)
class NotionConfig:
    token: str
    page_id: str = DEFAULT_PAGE_ID
    timeout_seconds: float = 15.0
    # page_id 직접 접근 실패 시 워크스페이스 검색(/v1/search)에 쓸 제목 키워드.
    search_query: str = ""

    @classmethod
    def from_files(cls, *, project_root: Path) -> "NotionConfig":
        """``config/kis_api.yaml``의 ``notion`` 섹션에서 토큰/페이지ID 로드.

        우선순위(토큰): yaml ``notion.token`` → env ``NOTION_TOKEN``.
        우선순위(페이지): yaml ``notion.page_id`` → env ``NOTION_PAGE_ID`` → 기본값.
        """
        token = ""
        page_id = ""
        search_query = ""
        # 키/비밀은 key/ 폴더 우선, 없으면 기존 config/ 폴백(하위호환·테스트).
        kis_path = project_root / "key" / "kis_api.yaml"
        if not kis_path.exists():
            kis_path = project_root / "config" / "kis_api.yaml"
        if kis_path.exists():
            try:
                doc = yaml.safe_load(kis_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                doc = {}
            notion = doc.get("notion") or {}
            if isinstance(notion, dict):
                token = notion.get("token") or notion.get("notion_token") or ""
                page_id = notion.get("page_id") or notion.get("notion_page_id") or ""
                search_query = notion.get("search_query") or ""
            # 평면 키 폴백(notion_token / notion_page_id at top level)
            token = token or doc.get("notion_token") or ""
            page_id = page_id or doc.get("notion_page_id") or ""

        if _is_placeholder(token):
            token = os.getenv("NOTION_TOKEN") or token
        if not page_id:
            page_id = os.getenv("NOTION_PAGE_ID") or DEFAULT_PAGE_ID
        search_query = os.getenv("NOTION_SEARCH_QUERY", search_query) or ""

        if _is_placeholder(token):
            raise NotionAuthError(
                "Notion 토큰이 없습니다. config/kis_api.yaml 의 notion.token 또는 "
                "환경변수 NOTION_TOKEN 을 설정하세요."
            )
        return cls(
            token=str(token), page_id=str(page_id), search_query=str(search_query)
        )


@dataclass
class NotionSection:
    """헤딩(또는 자식 페이지 제목) 하나와 그에 속한 텍스트 라인들."""

    heading: str
    lines: list[str] = field(default_factory=list)
    depth: int = 0

    def text(self) -> str:
        body = "\n".join(self.lines)
        return f"{self.heading}\n{body}".strip()


@dataclass
class NotionPage:
    page_id: str
    title: str
    sections: list[NotionSection]

    @property
    def markdown(self) -> str:
        return "\n\n".join(s.text() for s in self.sections if s.text())

    @property
    def line_count(self) -> int:
        return sum(len(s.lines) for s in self.sections)


_HEADING_TYPES = {"heading_1", "heading_2", "heading_3"}


def _rich_text(block: dict, block_type: str) -> str:
    payload = block.get(block_type) or {}
    rich = payload.get("rich_text") or payload.get("title") or []
    return "".join(r.get("plain_text", "") for r in rich).strip()


def _block_line(block: dict) -> tuple[str, bool]:
    """블록 → (텍스트 라인, is_heading). 빈/무시 블록은 ("", False)."""
    t = block.get("type", "")
    if t == "child_page":
        return (block.get("child_page", {}).get("title", "").strip(), True)
    text = _rich_text(block, t)
    if not text:
        return ("", False)
    if t in _HEADING_TYPES:
        return (text, True)
    prefix = {
        "bulleted_list_item": "• ",
        "numbered_list_item": "1. ",
        "to_do": "[ ] ",
        "toggle": "▸ ",
        "quote": "> ",
        "callout": "💡 ",
        "code": "",
        "paragraph": "",
    }.get(t, "")
    return (prefix + text, False)


def _page_obj_title(page: dict) -> str:
    """검색(/v1/search) 결과 page 객체에서 제목 텍스트를 추출한다."""
    props = page.get("properties") or {}
    for prop in props.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            rich = prop.get("title") or []
            return "".join(r.get("plain_text", "") for r in rich).strip()
    return ""


class NotionClient:
    """Notion 페이지를 재귀적으로 읽어 ``NotionPage``로 반환한다."""

    def __init__(
        self,
        config: NotionConfig,
        *,
        max_depth: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._cfg = config
        self._max_depth = max_depth
        self._transport = transport  # 테스트용 MockTransport 주입
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "NotionClient":
        self._client = httpx.AsyncClient(
            base_url=NOTION_API,
            timeout=self._cfg.timeout_seconds,
            headers={
                "Authorization": f"Bearer {self._cfg.token}",
                "Notion-Version": NOTION_VERSION,
            },
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require(self) -> httpx.AsyncClient:
        if self._client is None:
            raise NotionError("NotionClient는 async context manager로 사용하세요.")
        return self._client

    async def _get(self, path: str) -> dict:
        client = self._require()
        try:
            resp = await client.get(path)
        except httpx.HTTPError as exc:
            raise NotionError(f"Notion 연결 실패: {exc}") from exc
        if resp.status_code in (401, 403):
            raise NotionAuthError(
                f"Notion 인증 실패({resp.status_code}) — 토큰이 만료/무효이거나 "
                f"권한이 없습니다."
            )
        if resp.status_code == 404:
            # object_not_found: 거의 항상 '페이지가 통합에 공유 안 됨' 또는 'page_id 오타'.
            raise NotionError(
                "Notion 404: 페이지/블록을 찾을 수 없습니다 — page_id가 틀렸거나, "
                "해당 Notion 페이지가 Integration에 공유되지 않았습니다 "
                "(Notion 페이지 우측 상단 ··· → 연결(Connections)에서 통합 추가). "
                f"상세: {resp.text[:160]}"
            )
        if resp.status_code >= 400:
            raise NotionError(f"Notion {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    async def _post(self, path: str, payload: dict) -> dict:
        client = self._require()
        try:
            resp = await client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise NotionError(f"Notion 연결 실패: {exc}") from exc
        if resp.status_code in (401, 403):
            raise NotionAuthError(
                f"Notion 인증 실패({resp.status_code}) — 토큰이 만료/무효이거나 "
                f"권한이 없습니다."
            )
        if resp.status_code >= 400:
            raise NotionError(f"Notion {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    async def search_page(self, query: str = "") -> str | None:
        """워크스페이스 검색(/v1/search)으로 통합에 공유된 페이지를 찾아 첫 id 반환.

        page_id 직접 접근이 404일 때의 폴백. Notion /v1/search 는 통합에 '연결된'
        객체만 돌려주므로, 제목에 query 가 포함된 페이지를 우선 고르고 없으면 첫
        페이지를 택한다. 결과가 없으면(=공유된 페이지 없음) None.
        """
        payload: dict = {
            "filter": {"value": "page", "property": "object"},
            "page_size": 20,
        }
        if query:
            payload["query"] = query
        try:
            data = await self._post("/v1/search", payload)
        except NotionError:
            return None
        results = data.get("results") or []
        first: str | None = None
        for r in results:
            if r.get("object") != "page":
                continue
            if first is None:
                first = r.get("id")
            if query and query in _page_obj_title(r):
                return r.get("id")
        return first

    async def _page_title(self, page_uuid: str) -> str:
        try:
            page = await self._get(f"/v1/pages/{page_uuid}")
        except NotionError:
            return ""
        props = page.get("properties") or {}
        for prop in props.values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                rich = prop.get("title") or []
                return "".join(r.get("plain_text", "") for r in rich).strip()
        return ""

    async def _children(self, block_id: str) -> list[dict]:
        out: list[dict] = []
        cursor: str | None = None
        while True:
            qs = "?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            data = await self._get(f"/v1/blocks/{block_id}/children{qs}")
            out.extend(data.get("results") or [])
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return out

    async def _walk(
        self,
        block_id: str,
        sections: list[NotionSection],
        depth: int,
    ) -> None:
        if depth > self._max_depth:
            return
        for block in await self._children(block_id):
            line, is_heading = _block_line(block)
            btype = block.get("type", "")
            if btype == "child_page":
                title = line or "(제목 없음)"
                sections.append(NotionSection(heading=title, depth=depth))
                if block.get("id"):
                    await self._walk(block["id"], sections, depth + 1)
                continue
            if is_heading:
                sections.append(NotionSection(heading=line, depth=depth))
            elif line:
                if not sections:
                    sections.append(NotionSection(heading="(본문)", depth=depth))
                sections[-1].lines.append(line)
            if block.get("has_children") and btype not in _HEADING_TYPES:
                await self._walk(block["id"], sections, depth + 1)

    async def _collect_page(self, uuid: str) -> NotionPage:
        title = await self._page_title(uuid)
        sections: list[NotionSection] = []
        await self._walk(uuid, sections, 0)
        if not title:
            title = sections[0].heading if sections else "(제목 없음)"
        return NotionPage(page_id=uuid, title=title, sections=sections)

    async def fetch_page(self, page_id: str | None = None) -> NotionPage:
        """페이지(+자식 페이지) 전체를 수집한다.

        직접 page_id 접근이 실패(404/권한)하면, 워크스페이스 검색(/v1/search)으로
        통합에 공유된 페이지를 재탐색해 그 결과로 폴백한다. 검색으로도 다른 접근
        가능한 페이지를 못 찾으면 원래 오류를 그대로 올린다(상위에서 저장본 폴백).
        """
        raw_id = page_id or self._cfg.page_id
        uuid = _to_uuid(raw_id)
        try:
            return await self._collect_page(uuid)
        except NotionError as exc:
            found = await self.search_page(self._cfg.search_query)
            if found and _to_uuid(found) != uuid:
                return await self._collect_page(_to_uuid(found))
            raise exc
