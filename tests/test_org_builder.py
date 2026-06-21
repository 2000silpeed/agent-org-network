"""T5.3 Org 그래프 · Agent 빌더 — 결정론 테스트.

red → green 순서:
1. serialize_org_graph — Registry 주입 → {nodes, edges} 단언.
2. validate_card_for_builder — 유효/무효 카드 검증 + YAML round-trip.
3. 라우트 — TestClient(세션 secret 주입) → GET /org/graph · POST /builder/validate 인증 게이트.
4. HTML 파일 라우트 — GET /org/view · GET /builder 200.
"""

from __future__ import annotations

from datetime import date
from typing import Any, cast

import yaml
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.agent_card import AgentCard
from agent_org_network.registry import Registry
from agent_org_network.runtime import StubRuntime
from agent_org_network.user import User
from agent_org_network.web import (
    BuilderValidateRequest,
    create_app,
    serialize_org_graph,
    validate_card_for_builder,
)

_SECRET = "test-secret"
_DATE = date(2026, 6, 20)


# ── 고정 소형 Registry 팩토리 ────────────────────────────────────────────────


def _make_registry(*, with_maintainer: bool = False) -> Registry:
    """테스트용 작은 Registry — 유저 2명 + 카드 2장 (결정론)."""
    reg = Registry()
    reg.register_user(User(id="root_mgr"))
    reg.register_user(User(id="cs_lead", manager="root_mgr"))

    reg.register(
        AgentCard(
            agent_id="cs_ops",
            owner="cs_lead",
            team="cs",
            summary="환불 안내",
            domains=["환불", "보상"],
            last_reviewed_at=_DATE,
            knowledge_sources=["위키/환불정책"],
        )
    )
    card2 = AgentCard(
        agent_id="legal_ops",
        owner="cs_lead",
        team="legal",
        summary="계약 안내",
        domains=["계약 검토"],
        last_reviewed_at=_DATE,
        maintainer="root_mgr" if with_maintainer else None,
    )
    reg.register(card2)
    reg.validate()
    return reg


def _demo_registry() -> Registry:
    """데모와 동일한 6유저 5카드 Registry."""
    from agent_org_network.demo import build_demo
    from agent_org_network.runtime import StubRuntime

    bundle = build_demo(runtime=StubRuntime())
    return bundle.registry


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────


def _get(client: TestClient, url: str) -> tuple[int, Any]:
    http: Any = client
    res: Response = cast(Response, http.get(url))
    try:
        body: Any = res.json()
    except Exception:
        body = res.text
    return res.status_code, body


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> tuple[int, Any]:
    http: Any = client
    res: Response = cast(Response, http.post(url, json=payload))
    try:
        body: Any = res.json()
    except Exception:
        body = res.text
    return res.status_code, body


def _login(client: TestClient, user_id: str) -> None:
    status, _ = _post(client, "/login", {"user_id": user_id})
    assert status == 200, f"로그인 실패: {user_id}"


# ════════════════════════════════════════════════════════════════════════════
# 1. serialize_org_graph — 순수 함수
# ════════════════════════════════════════════════════════════════════════════


class TestSerializeOrgGraph:
    def test_소형_registry_노드_수(self) -> None:
        reg = _make_registry()
        result = serialize_org_graph(reg)
        nodes: list[dict[str, Any]] = result["nodes"]
        user_nodes = [n for n in nodes if n["type"] == "user"]
        card_nodes = [n for n in nodes if n["type"] == "card"]
        assert len(user_nodes) == 2  # root_mgr, cs_lead
        assert len(card_nodes) == 2  # cs_ops, legal_ops

    def test_user_노드_구조(self) -> None:
        reg = _make_registry()
        result = serialize_org_graph(reg)
        nodes: list[dict[str, Any]] = result["nodes"]
        user_nodes = {n["id"]: n for n in nodes if n["type"] == "user"}
        assert "root_mgr" in user_nodes
        assert "cs_lead" in user_nodes
        # cs_lead는 manager=root_mgr
        assert user_nodes["cs_lead"]["manager"] == "root_mgr"
        # root_mgr는 manager 없음 — 키 없거나 None
        root_node = user_nodes["root_mgr"]
        assert root_node.get("manager") is None

    def test_card_노드_구조(self) -> None:
        reg = _make_registry()
        result = serialize_org_graph(reg)
        nodes: list[dict[str, Any]] = result["nodes"]
        card_nodes = {n["agent_id"]: n for n in nodes if n["type"] == "card"}
        assert "cs_ops" in card_nodes
        cs = card_nodes["cs_ops"]
        assert cs["owner"] == "cs_lead"
        assert cs["team"] == "cs"
        assert "환불" in cs["domains"]

    def test_owns_엣지_수_equals_카드_수(self) -> None:
        reg = _make_registry()
        result = serialize_org_graph(reg)
        edges: list[dict[str, Any]] = result["edges"]
        owns_edges = [e for e in edges if e["type"] == "owns"]
        all_cards = reg.all_cards()
        assert len(owns_edges) == len(all_cards)

    def test_manages_엣지_루트_제외(self) -> None:
        """manager가 있는 유저만 manages 엣지 — root_mgr는 없음."""
        reg = _make_registry()
        result = serialize_org_graph(reg)
        edges: list[dict[str, Any]] = result["edges"]
        manages_edges = [e for e in edges if e["type"] == "manages"]
        # cs_lead만 manager 있음
        assert len(manages_edges) == 1
        assert manages_edges[0]["source"] == "root_mgr"
        assert manages_edges[0]["target"] == "cs_lead"

    def test_maintainer_없으면_maintains_엣지_0(self) -> None:
        reg = _make_registry(with_maintainer=False)
        result = serialize_org_graph(reg)
        edges: list[dict[str, Any]] = result["edges"]
        maintains_edges = [e for e in edges if e["type"] == "maintains"]
        assert len(maintains_edges) == 0

    def test_maintainer_있으면_maintains_엣지_1(self) -> None:
        reg = _make_registry(with_maintainer=True)
        result = serialize_org_graph(reg)
        edges: list[dict[str, Any]] = result["edges"]
        maintains_edges = [e for e in edges if e["type"] == "maintains"]
        assert len(maintains_edges) == 1
        assert maintains_edges[0]["source"] == "root_mgr"
        assert maintains_edges[0]["target"] == "legal_ops"

    def test_결과에_nodes_edges_키가_있다(self) -> None:
        reg = _make_registry()
        result = serialize_org_graph(reg)
        assert "nodes" in result
        assert "edges" in result

    def test_노드_정렬_결정론(self) -> None:
        """같은 registry → 두 번 호출해도 노드 순서 동일."""
        reg = _make_registry()
        r1 = serialize_org_graph(reg)
        r2 = serialize_org_graph(reg)
        assert [n.get("id") or n.get("agent_id") for n in r1["nodes"]] == [
            n.get("id") or n.get("agent_id") for n in r2["nodes"]
        ]

    def test_데모_registry_6유저_5카드(self) -> None:
        """데모 레지스트리 → 유저 6 + 카드 5 노드."""
        reg = _demo_registry()
        result = serialize_org_graph(reg)
        nodes: list[dict[str, Any]] = result["nodes"]
        user_nodes = [n for n in nodes if n["type"] == "user"]
        card_nodes = [n for n in nodes if n["type"] == "card"]
        assert len(user_nodes) == 6
        assert len(card_nodes) == 5

    def test_데모_owns_엣지_수는_5(self) -> None:
        reg = _demo_registry()
        result = serialize_org_graph(reg)
        edges: list[dict[str, Any]] = result["edges"]
        owns_edges = [e for e in edges if e["type"] == "owns"]
        assert len(owns_edges) == 5

    def test_데모_manages_엣지_5(self) -> None:
        """데모에서 root_manager 제외 5명이 manager 있음 → manages 5."""
        reg = _demo_registry()
        result = serialize_org_graph(reg)
        edges: list[dict[str, Any]] = result["edges"]
        manages_edges = [e for e in edges if e["type"] == "manages"]
        assert len(manages_edges) == 5

    def test_데모_maintains_엣지_0(self) -> None:
        """데모 카드는 maintainer 없음 → maintains 0."""
        reg = _demo_registry()
        result = serialize_org_graph(reg)
        edges: list[dict[str, Any]] = result["edges"]
        maintains_edges = [e for e in edges if e["type"] == "maintains"]
        assert len(maintains_edges) == 0


# ════════════════════════════════════════════════════════════════════════════
# 2. validate_card_for_builder — 순수 함수
# ════════════════════════════════════════════════════════════════════════════


class TestValidateCardForBuilder:
    def _req(self, **kwargs: Any) -> BuilderValidateRequest:
        defaults: dict[str, Any] = {
            "agent_id": "new_ops",
            "owner": "cs_lead",
            "team": "cs",
            "summary": "신규 카드",
            "domains": ["환불"],
            "last_reviewed_at": "2026-06-20",
        }
        defaults.update(kwargs)
        return BuilderValidateRequest(**defaults)

    def test_유효_카드_ok_True(self) -> None:
        reg = _make_registry()
        req = self._req()
        result = validate_card_for_builder(req, reg)
        assert result["ok"] is True
        assert "yaml" in result

    def test_유효_카드_yaml_round_trip(self) -> None:
        """ok:True → yaml safe_load → AgentCard 재구성 가능."""
        reg = _make_registry()
        req = self._req()
        result = validate_card_for_builder(req, reg)
        assert result["ok"] is True
        yaml_text: str = result["yaml"]
        parsed = yaml.safe_load(yaml_text)
        card = AgentCard.model_validate(parsed)
        assert card.agent_id == "new_ops"
        assert card.owner == "cs_lead"
        assert "환불" in card.domains

    def test_잘못된_date_형식_ok_False(self) -> None:
        """last_reviewed_at이 ISO date가 아니면 pydantic 실패 → ok:False."""
        reg = _make_registry()
        req = self._req(last_reviewed_at="not-a-date")
        result = validate_card_for_builder(req, reg)
        assert result["ok"] is False
        assert len(result["errors"]) > 0

    def test_미등록_owner_ok_False(self) -> None:
        reg = _make_registry()
        req = self._req(owner="nobody_unknown")
        result = validate_card_for_builder(req, reg)
        assert result["ok"] is False
        errors: list[str] = result["errors"]
        assert any("owner" in e for e in errors)

    def test_미등록_maintainer_ok_False(self) -> None:
        reg = _make_registry()
        req = self._req(maintainer="ghost_user")
        result = validate_card_for_builder(req, reg)
        assert result["ok"] is False
        errors: list[str] = result["errors"]
        assert any("maintainer" in e for e in errors)

    def test_등록된_maintainer_ok_True(self) -> None:
        reg = _make_registry()
        req = self._req(maintainer="root_mgr")
        result = validate_card_for_builder(req, reg)
        assert result["ok"] is True

    def test_빈_agent_id_ok_False(self) -> None:
        """agent_id가 빈 문자열이면 pydantic 거부 또는 admission 거부."""
        reg = _make_registry()
        req = self._req(agent_id="")
        result = validate_card_for_builder(req, reg)
        # agent_id=="" 는 str 이므로 pydantic 통과 여부를 확인:
        # AgentCard는 agent_id 빈 str을 허용하므로 ok:True일 수 있음
        # → 도메인 정책으로 거부 여부는 구현 결정이지만 테스트는 결과만 단언.
        # 단: 이 테스트는 "결과가 ok:False이어야 한다"고 강제한다(빈 agent_id 거부).
        assert result["ok"] is False

    def test_yaml_unicode_허용(self) -> None:
        """한글 summary가 YAML에서 깨지지 않는다(allow_unicode=True)."""
        reg = _make_registry()
        req = self._req(summary="한글 요약 — 한국어 처리 안내")
        result = validate_card_for_builder(req, reg)
        assert result["ok"] is True
        assert "한글 요약" in result["yaml"]

    def test_선택_필드_빈_리스트는_yaml에서_생략_가능(self) -> None:
        """빈 can_answer 등은 YAML에서 없거나 빈 리스트 — round-trip 통과면 OK."""
        reg = _make_registry()
        req = self._req(can_answer=[], cannot_answer=[])
        result = validate_card_for_builder(req, reg)
        assert result["ok"] is True
        parsed = yaml.safe_load(result["yaml"])
        card = AgentCard.model_validate(parsed)
        assert card.can_answer == []


# ════════════════════════════════════════════════════════════════════════════
# 3. 라우트 — create_app + TestClient
# ════════════════════════════════════════════════════════════════════════════


class TestOrgGraphRoute:
    """GET /org/graph 인증 게이트 + 데이터."""

    def test_미로그인_org_graph_401(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        status, _ = _get(client, "/org/graph")
        assert status == 401

    def test_로그인_후_org_graph_200(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "cs_lead")
        status, body = _get(client, "/org/graph")
        assert status == 200
        assert "nodes" in body
        assert "edges" in body

    def test_로그인_후_org_graph_nodes_edges_있음(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "cs_lead")
        status, body = _get(client, "/org/graph")
        assert status == 200
        nodes: list[Any] = body["nodes"]
        edges: list[Any] = body["edges"]
        assert len(nodes) > 0
        assert len(edges) > 0

    def test_인증_OFF_org_graph_200(self) -> None:
        """session_secret 미주입이면 인증 없이 접근 가능."""
        app = create_app(runtime=StubRuntime())
        client = TestClient(app)
        status, body = _get(client, "/org/graph")
        assert status == 200
        assert "nodes" in body


class TestBuilderValidateRoute:
    """POST /builder/validate 인증 게이트 + Owner 스코프."""

    def _valid_payload(self, owner: str = "cs_lead") -> dict[str, Any]:
        return {
            "agent_id": "new_ops",
            "owner": owner,
            "team": "cs",
            "summary": "신규 카드",
            "domains": ["환불"],
            "last_reviewed_at": "2026-06-20",
        }

    def test_미로그인_builder_validate_401(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        status, _ = _post(client, "/builder/validate", self._valid_payload())
        assert status == 401

    def test_세션_owner_일치_200_ok(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "cs_lead")
        status, body = _post(client, "/builder/validate", self._valid_payload("cs_lead"))
        assert status == 200
        assert body["ok"] is True

    def test_세션_owner_불일치_403(self) -> None:
        """세션 cs_lead이지만 owner=legal_lead 카드 구성 시도 → 403."""
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "cs_lead")
        status, _ = _post(client, "/builder/validate", self._valid_payload("legal_lead"))
        assert status == 403

    def test_인증_OFF_builder_validate_자유(self) -> None:
        """session_secret 미주입이면 owner 무관 접근 가능."""
        app = create_app(runtime=StubRuntime())
        client = TestClient(app)
        status, _ = _post(client, "/builder/validate", self._valid_payload("cs_lead"))
        assert status == 200

    def test_인증_OFF_미등록_owner_ok_False(self) -> None:
        """인증 OFF여도 admission 검증은 동작한다."""
        app = create_app(runtime=StubRuntime())
        client = TestClient(app)
        payload = self._valid_payload("nobody_ghost")
        status, body = _post(client, "/builder/validate", payload)
        assert status == 200  # HTTP 200이지만
        assert body["ok"] is False  # 내부 검증 실패


class TestOrgBuilderHtmlRoute:
    """GET /org/view · GET /builder → 200 (HTML FileResponse)."""

    def test_org_view_200(self) -> None:
        app = create_app(runtime=StubRuntime())
        client = TestClient(app)
        http: Any = client
        res: Response = cast(Response, http.get("/org/view"))
        assert res.status_code == 200

    def test_builder_get_200(self) -> None:
        app = create_app(runtime=StubRuntime())
        client = TestClient(app)
        http: Any = client
        res: Response = cast(Response, http.get("/builder"))
        assert res.status_code == 200
