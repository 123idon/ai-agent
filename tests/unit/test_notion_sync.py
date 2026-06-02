"""학습부 노션 동기화 단위 테스트.

- NotionClient: httpx.MockTransport로 Notion API(페이지/블록) 응답을 모킹해 재귀 수집 검증.
- classify_knowledge: 5개 부서 카테고리 규칙 분류 검증.
- NotionSyncAgent: 저장/변경 감지/업데이트 로그/status() 검증(가짜 클라이언트 주입).
- NotionKnowledgeView: 에이전트별 조회.
- NotionConfig: yaml placeholder → env 폴백, 토큰 누락 시 예외.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agents.learning.notion_sync import NotionSyncAgent
from core.notion_client import (
    NotionAuthError,
    NotionClient,
    NotionConfig,
    NotionError,
    NotionKnowledgeView,
    NotionPage,
    NotionSection,
    classify_knowledge,
)
from core.notion_client.client import _to_uuid

PAGE_ID = "35a0717882e381ce8fc3d257a5c24e4b"
UUID = _to_uuid(PAGE_ID)


# ─────────────────────────── NotionClient (MockTransport) ───────────────────────────


def _notion_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == f"/v1/pages/{UUID}":
        return httpx.Response(200, json={
            "properties": {"title": {"type": "title",
                                     "title": [{"plain_text": "단타 커리큘럼"}]}},
        })
    if path == f"/v1/blocks/{UUID}/children":
        return httpx.Response(200, json={
            "results": [
                {"type": "heading_1", "id": "h1",
                 "heading_1": {"rich_text": [{"plain_text": "손절 익절 기준"}]}},
                {"type": "paragraph", "id": "p1",
                 "paragraph": {"rich_text": [{"plain_text": "손절은 시장가로 즉시 실행한다"}]}},
                {"type": "child_page", "id": "child1", "has_children": True,
                 "child_page": {"title": "Phase 5 보조지표"}},
            ],
            "has_more": False, "next_cursor": None,
        })
    if path == "/v1/blocks/child1/children":
        return httpx.Response(200, json={
            "results": [
                {"type": "heading_2", "id": "h2",
                 "heading_2": {"rich_text": [{"plain_text": "RSI 심화"}]}},
                {"type": "bulleted_list_item", "id": "b1",
                 "bulleted_list_item": {"rich_text": [{"plain_text": "RSI 60 돌파 시 매수 모멘텀 확인"}]}},
            ],
            "has_more": False, "next_cursor": None,
        })
    return httpx.Response(404, json={"object": "error", "message": "not found"})


async def test_notion_client_recursive_fetch() -> None:
    cfg = NotionConfig(token="ntn_testtoken1234567890", page_id=PAGE_ID)
    transport = httpx.MockTransport(_notion_handler)
    async with NotionClient(cfg, transport=transport) as client:
        page = await client.fetch_page()

    assert page.title == "단타 커리큘럼"
    headings = [s.heading for s in page.sections]
    assert "손절 익절 기준" in headings
    assert "Phase 5 보조지표" in headings   # 자식 페이지까지 재귀
    assert "RSI 심화" in headings
    # 본문 라인이 가장 가까운 섹션에 귀속
    assert any("손절은 시장가" in line for s in page.sections for line in s.lines)
    assert any("RSI 60 돌파" in line for s in page.sections for line in s.lines)


async def test_notion_client_auth_error() -> None:
    cfg = NotionConfig(token="ntn_bad", page_id=PAGE_ID)
    transport = httpx.MockTransport(lambda r: httpx.Response(401, json={"message": "unauthorized"}))
    with pytest.raises(NotionAuthError):
        async with NotionClient(cfg, transport=transport) as client:
            await client.fetch_page()


# ─────────────────────────── classifier ───────────────────────────


def _page(*sections: NotionSection) -> NotionPage:
    return NotionPage(page_id=UUID, title="t", sections=list(sections))


def test_classifier_routes_to_departments() -> None:
    page = _page(
        NotionSection(heading="종목 스크리닝", lines=[
            "거래대금 상위 + 거래량 전일 대비 300% 이상만 후보로",
            "관리종목/거래정지 종목은 제외한다",
        ]),
        NotionSection(heading="매수 진입 조건", lines=[
            "RSI 60 돌파 + MACD 골든크로스에서 진입",
            "볼린저밴드 수축 후 거래량 동반 돌파",
        ]),
        NotionSection(heading="포지션 청산 전략", lines=[
            "하드 손절 -3%, 1차 익절 +3%에서 50% 청산",
            "고점 대비 -1.5% 트레일링 스탑",
        ]),
        NotionSection(heading="시장 환경 점검", lines=[
            "VIX 30 이상이면 신규 진입 자제, 야간 선물 방향 확인",
            "14:30 이후 신규 진입 금지",
        ]),
        NotionSection(heading="매매 원칙", lines=[
            "매매 일지 복기로 원칙 준수 여부를 평가한다",
        ]),
    )
    knowledge = classify_knowledge(page)
    cats = knowledge["categories"]
    assert cats["screening"]["count"] >= 1
    assert cats["signal"]["count"] >= 1
    assert cats["risk"]["count"] >= 1
    assert cats["market_watch"]["count"] >= 1
    assert cats["ceo"]["count"] >= 1

    def _texts(key: str) -> str:
        return " ".join(r["text"] for r in cats[key]["rules"])

    assert "거래대금" in _texts("screening")
    assert "골든크로스" in _texts("signal")
    assert "손절" in _texts("risk")
    assert "VIX" in _texts("market_watch")
    assert "복기" in _texts("ceo")
    assert knowledge["stats"]["total_rules"] == sum(
        cats[k]["count"] for k in ("screening", "signal", "risk", "market_watch", "ceo")
    )


# ─────────────────────────── NotionSyncAgent ───────────────────────────


class _FakeClient:
    """NotionClient duck-type — async ctx manager + fetch_page."""

    def __init__(self, page: NotionPage) -> None:
        self._page = page

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def fetch_page(self, page_id: str | None = None) -> NotionPage:
        return self._page


def _sample_page(extra_line: str = "") -> NotionPage:
    lines = ["하드 손절 -3% 즉시 청산", "1차 익절 +3% 50% 청산"]
    if extra_line:
        lines.append(extra_line)
    return _page(
        NotionSection(heading="청산 전략", lines=lines),
        NotionSection(heading="진입 조건", lines=["RSI 60 돌파 매수"]),
    )


def _agent(page: NotionPage, tmp_path: Path, counter: dict | None = None) -> NotionSyncAgent:
    cfg = NotionConfig(token="ntn_testtoken1234567890", page_id=PAGE_ID)
    # clock을 단조 증가 카운터로 고정(결정론적 타임스탬프).
    state = counter if counter is not None else {"n": 0}

    def _clock() -> str:
        state["n"] += 1
        return f"2026-05-30T10:00:{state['n']:02d}"

    return NotionSyncAgent(
        cfg, memory_dir=tmp_path, clock=_clock,
        client_factory=lambda _c: _FakeClient(page),
    )


async def test_sync_writes_and_detects_change(tmp_path: Path) -> None:
    counter = {"n": 0}
    agent = _agent(_sample_page(), tmp_path, counter)

    # 1차: 최초 동기화 → changed True, 파일/로그 생성
    r1 = await agent.sync()
    assert r1["ok"] and r1["changed"]
    kpath = tmp_path / "notion_knowledge.json"
    assert kpath.exists()
    data = json.loads(kpath.read_text(encoding="utf-8"))
    assert data["categories"]["risk"]["count"] >= 1
    log_lines = (tmp_path / "notion_updates.log").read_text(encoding="utf-8").splitlines()
    assert any("INITIAL" in ln for ln in log_lines)

    # 2차: 동일 내용 → changed False, 로그 추가 없음
    r2 = await agent.sync()
    assert r2["ok"] and not r2["changed"]
    log_lines2 = (tmp_path / "notion_updates.log").read_text(encoding="utf-8").splitlines()
    assert len(log_lines2) == len(log_lines)

    # 3차: 내용 변경 → changed True, CHANGED 로그 추가
    agent2 = NotionSyncAgent(
        agent._cfg, memory_dir=tmp_path, clock=agent._clock,
        client_factory=lambda _c: _FakeClient(_sample_page("타임스톱 30분 재평가")),
    )
    r3 = await agent2.sync()
    assert r3["ok"] and r3["changed"]
    log_lines3 = (tmp_path / "notion_updates.log").read_text(encoding="utf-8").splitlines()
    assert any("CHANGED" in ln for ln in log_lines3)


async def test_sync_force_rewrites_unchanged(tmp_path: Path) -> None:
    agent = _agent(_sample_page(), tmp_path)
    await agent.sync()
    r = await agent.sync(force=True)
    assert r["ok"] and r["changed"]


async def test_status_shape(tmp_path: Path) -> None:
    agent = _agent(_sample_page(), tmp_path)
    # 미동기화 상태
    st0 = agent.status()
    assert st0["ok"] and not st0["synced"]
    # 동기화 후
    await agent.sync()
    st = agent.status()
    assert st["ok"] and st["synced"]
    assert st["total_rules"] >= 2
    assert set(st["agents"]) == {"screening", "signal", "risk", "market_watch", "ceo"}
    assert st["agents"]["risk"]["count"] >= 1
    assert st["updates"]


# ─────────────────────────── NotionKnowledgeView ───────────────────────────


async def test_knowledge_view_for_agent(tmp_path: Path) -> None:
    agent = _agent(_sample_page(), tmp_path)
    await agent.sync()

    view = NotionKnowledgeView.load_path(tmp_path / "notion_knowledge.json")
    assert view.available
    risk = view.for_agent("risk.risk_manager")
    assert risk["count"] >= 1
    assert any("손절" in r["text"] for r in risk["rules"])
    # 별칭/short key
    assert view.for_agent("signal")["count"] >= 1
    assert view.summary_line("risk.risk_manager")


def test_knowledge_view_missing_file_is_empty(tmp_path: Path) -> None:
    view = NotionKnowledgeView.load(tmp_path)
    assert not view.available
    assert view.for_agent("risk.risk_manager")["rules"] == []
    assert view.summary_line("ceo") == ""


# ─────────────────────────── NotionConfig ───────────────────────────


def _write_kis_yaml(root: Path, token: str) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "kis_api.yaml").write_text(
        f'notion:\n  token: "{token}"\n  page_id: "{PAGE_ID}"\n',
        encoding="utf-8",
    )


def test_config_env_fallback_on_placeholder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_kis_yaml(tmp_path, "ntn_여기에_Notion_Integration_Secret")  # placeholder
    monkeypatch.setenv("NOTION_TOKEN", "ntn_realtoken_abcdef123456")
    cfg = NotionConfig.from_files(project_root=tmp_path)
    assert cfg.token == "ntn_realtoken_abcdef123456"
    assert cfg.page_id == PAGE_ID


def test_config_reads_yaml_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    _write_kis_yaml(tmp_path, "ntn_realyamltoken_987654321")
    cfg = NotionConfig.from_files(project_root=tmp_path)
    assert cfg.token == "ntn_realyamltoken_987654321"


def test_config_missing_token_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    _write_kis_yaml(tmp_path, "ntn_여기에_Notion_Integration_Secret")
    with pytest.raises(NotionAuthError):
        NotionConfig.from_files(project_root=tmp_path)


# ─────────────────────────── 폴백 1: 워크스페이스 검색(/v1/search) ───────────────────────────


FOUND_UUID = _to_uuid("aaaaaaaabbbbccccddddeeeeeeeeeeee")


def _search_fallback_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    # 설정된 page_id 블록 접근은 404 (통합에 페이지가 공유되지 않은 상황 재현)
    if path == f"/v1/blocks/{UUID}/children":
        return httpx.Response(404, json={
            "object": "error", "status": 404, "code": "object_not_found",
            "message": f"Could not find block with ID: {UUID}"})
    # 워크스페이스 검색 → 통합에 공유된 다른 페이지를 반환
    if path == "/v1/search":
        return httpx.Response(200, json={"results": [{
            "object": "page", "id": FOUND_UUID,
            "properties": {"title": {"type": "title",
                                     "title": [{"plain_text": "단타 트레이딩 마스터 커리큘럼"}]}},
        }]})
    if path == f"/v1/pages/{FOUND_UUID}":
        return httpx.Response(200, json={
            "properties": {"title": {"type": "title",
                                     "title": [{"plain_text": "단타 트레이딩 마스터 커리큘럼"}]}}})
    if path == f"/v1/blocks/{FOUND_UUID}/children":
        return httpx.Response(200, json={
            "results": [{"type": "heading_1", "id": "h1",
                         "heading_1": {"rich_text": [{"plain_text": "손절 -3% 즉시 청산"}]}}],
            "has_more": False, "next_cursor": None})
    return httpx.Response(404, json={"object": "error", "message": "nf"})


async def test_search_fallback_when_page_inaccessible() -> None:
    cfg = NotionConfig(token="ntn_testtoken1234567890", page_id=PAGE_ID,
                       search_query="커리큘럼")
    transport = httpx.MockTransport(_search_fallback_handler)
    async with NotionClient(cfg, transport=transport) as client:
        page = await client.fetch_page()
    # 직접 접근(404) → 검색으로 찾은 페이지로 폴백
    assert page.page_id == FOUND_UUID
    assert page.title == "단타 트레이딩 마스터 커리큘럼"
    assert any("손절" in s.heading for s in page.sections)


async def test_search_returns_nothing_reraises_original() -> None:
    # 검색 결과가 비어 있으면(=공유된 페이지 없음) 원래 404 오류를 그대로 올린다.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/search":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404, json={
            "object": "error", "code": "object_not_found",
            "message": f"Could not find block with ID: {UUID}"})

    cfg = NotionConfig(token="ntn_testtoken1234567890", page_id=PAGE_ID)
    transport = httpx.MockTransport(handler)
    with pytest.raises(NotionError):
        async with NotionClient(cfg, transport=transport) as client:
            await client.fetch_page()


# ─────────────────────────── 폴백 2: 기존 저장본(notion_knowledge.json) ───────────────────────────


class _RaisingClient:
    """fetch_page가 항상 NotionError를 내는 가짜 클라이언트(접근 실패 재현)."""

    async def __aenter__(self) -> "_RaisingClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def fetch_page(self, page_id: str | None = None) -> NotionPage:
        raise NotionError("Notion 404: 접근 불가")


async def test_sync_falls_back_to_stored_knowledge(tmp_path: Path) -> None:
    # 1차: 정상 동기화로 저장본 생성
    await _agent(_sample_page(), tmp_path).sync()

    # 2차: 노션 접근 실패 → 저장본 폴백(ok True + fallback True, 데이터 유지)
    cfg = NotionConfig(token="ntn_testtoken1234567890", page_id=PAGE_ID)
    fail_agent = NotionSyncAgent(
        cfg, memory_dir=tmp_path, clock=lambda: "2026-05-30T11:00:00",
        client_factory=lambda _c: _RaisingClient(),
    )
    r = await fail_agent.sync(force=True)
    assert r["ok"] and r.get("fallback") and not r.get("changed")
    assert r["stats"]["total_rules"] >= 2          # 저장본 통계 유지
    data = json.loads((tmp_path / "notion_knowledge.json").read_text(encoding="utf-8"))
    assert data.get("fallback") is True            # 파일에 폴백 플래그 기록


async def test_sync_no_stored_knowledge_reports_error(tmp_path: Path) -> None:
    cfg = NotionConfig(token="ntn_testtoken1234567890", page_id=PAGE_ID)
    agent = NotionSyncAgent(
        cfg, memory_dir=tmp_path, clock=lambda: "2026-05-30T11:00:00",
        client_factory=lambda _c: _RaisingClient(),
    )
    r = await agent.sync(force=True)
    assert not r["ok"] and "error" in r            # 저장본 없으면 종전대로 실패 보고
