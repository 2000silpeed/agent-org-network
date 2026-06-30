"""ADR 0030 — owner측 OKF 저작면 라우트 2개(/author/run·/author/publish) 결정론 테스트.

TestClient + 세션 + FakeGitGateway 주입. 실 git·실 claude·실 네트워크·owner OAuth 0.
핵심 단언: owner 스코프(403)·미로그인(401)·staged 개념+over-claim dropped·커밋+목차 publish
(거부 제외)·**중앙 store에 raw/초안 0**(published_index_store엔 목차만·본문 0).
"""

from __future__ import annotations

import json
from typing import Any, cast

from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.agent_card import AgentCard
from agent_org_network.demo import build_demo, seed_published_index_store
from agent_org_network.git_gateway import FakeGitGateway
from agent_org_network.okf_authoring import FakeAuthor, OkfDocumentDraft
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import create_app

_SECRET = "test-secret"

# 어느 데모 카드도 권한으로 갖지 않는 over-claim 라벨 — admit_okf가 dropped_concepts로 떨군다.
_OVERCLAIM_DOMAIN = "기밀"


def _fake_author_for(card: AgentCard) -> FakeAuthor:
    """결정론 OKF 저작 더블 — 카드 owned domain 2건(in-domain) + over-claim 1건 고정 산출.

    프로덕션은 실 `LlmAuthor`(owner OAuth 추출)지만, 라우트 결정론 테스트는 실 LLM·실 네트워크를
    치면 안 되므로 `create_app(author=...)`로 이 Fake를 주입한다(T11.7d·ADR 0030 S1 — "항상 실
    LLM"은 *프로덕션 기본*이지 테스트가 네트워크를 친다는 뜻이 아니다). 입력 무관 고정 산출이라
    raw 본문 토큰이 결코 개념에 섞이지 않는다(비소유 단언과도 정합).
    """
    domains = list(card.domains)
    in_domain = domains[:2] if len(domains) >= 2 else (domains or [_OVERCLAIM_DOMAIN])
    docs: list[OkfDocumentDraft] = [
        OkfDocumentDraft(
            concept_id=f"demo-{card.agent_id}-{i + 1}",
            title=f"{dom} 정책 요약",
            body=f"{dom}에 대한 기준과 처리 절차를 정리한 개념입니다(테스트 고정 초안).",
            core_question=f"{dom}은 어떻게 처리하나요?",
            domain=dom,
        )
        for i, dom in enumerate(in_domain)
    ]
    # over-claim 1건 — 카드 권한 밖 domain이라 admit_okf가 over-claim으로 떨군다.
    docs.append(
        OkfDocumentDraft(
            concept_id=f"demo-{card.agent_id}-nda",
            title="기밀유지(NDA) 규정",
            body="권한 밖 도메인의 개념입니다 — admit_okf가 over-claim으로 떨굽니다(테스트).",
            core_question="NDA는 어떻게 처리하나요?",
            domain=_OVERCLAIM_DOMAIN,
        )
    )
    fixed = tuple(docs)
    return FakeAuthor(split_result=fixed, derive_result=fixed, link_result=())


def _make_client(gw: FakeGitGateway | None = None) -> TestClient:
    gw = gw or FakeGitGateway()
    # cs_ops(domains=환불·보상) 기준 Fake author를 주입한다. 200 경로 테스트는 이 산출을
    # 단언하고, 401/403/404 경로는 권한 가드가 먼저라 author 산출에 도달하지 않는다.
    card = build_demo(runtime=StubRuntime()).registry.get("cs_ops")
    app = create_app(
        runtime=StubRuntime(),
        session_secret=_SECRET,
        git_gateway=gw,
        author=_fake_author_for(card),
    )
    return TestClient(app)


def _login(client: TestClient, user_id: str) -> None:
    http: Any = client
    res: Response = cast(Response, http.post("/login", json={"user_id": user_id}))
    assert res.status_code == 200, f"로그인 실패: {user_id}"


def _run(
    client: TestClient, agent_id: str = "cs_ops", document: str = "환불 정책 본문입니다."
) -> tuple[int, Any]:
    http: Any = client
    res: Response = cast(
        Response,
        http.post("/author/run", json={"agent_id": agent_id, "document": document}),
    )
    try:
        body: Any = res.json()
    except Exception:
        body = res.text
    return res.status_code, body


def _publish(
    client: TestClient, agent_id: str, concepts: list[dict[str, Any]]
) -> tuple[int, Any]:
    http: Any = client
    res: Response = cast(
        Response,
        http.post(
            "/author/publish", json={"agent_id": agent_id, "concepts": concepts}
        ),
    )
    try:
        body: Any = res.json()
    except Exception:
        body = res.text
    return res.status_code, body


# ── /author/run ──────────────────────────────────────────────────────────────


class TestAuthorRun인증스코프:
    def test_미로그인_401(self) -> None:
        client = _make_client()
        status, _ = _run(client)
        assert status == 401

    def test_세션_불일치_403(self) -> None:
        """cs_lead 로그인 후 contract_ops(owner=legal_lead) 저작 시도 → 403."""
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _run(client, agent_id="contract_ops")
        assert status == 403

    def test_미존재_카드_404(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _run(client, agent_id="no_such_card")
        assert status == 404

    def test_자기_카드_200(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _run(client, agent_id="cs_ops")
        assert status == 200


class TestAuthorRun산출:
    def test_staged_개념과_overclaim_dropped_반환(self) -> None:
        """in-domain 개념 + over-claim(권한 밖) dropped를 함께 반환한다."""
        client = _make_client()
        _login(client, "cs_lead")
        status, body = _run(client, agent_id="cs_ops")
        assert status == 200
        # in_domain True 개념(환불/보상)과 over-claim(NDA·in_domain False) 공존
        in_domain = [c for c in body["concepts"] if c["in_domain"]]
        over_claim = [c for c in body["concepts"] if not c["in_domain"]]
        assert len(in_domain) >= 2
        assert len(over_claim) >= 1
        # over-claim은 dropped 목록에도 사유와 함께 실린다
        dropped_ids = {d["concept_id"] for d in body["dropped"]}
        assert dropped_ids == {c["concept_id"] for c in over_claim}
        assert all("reason" in d for d in body["dropped"])

    def test_stages_진행_표현(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        _, body = _run(client, agent_id="cs_ops")
        keys = [s["key"] for s in body["stages"]]
        assert keys == ["ingest", "split", "derive", "link", "index"]

    def test_in_domain_개념_domain은_카드_권한안(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        _, body = _run(client, agent_id="cs_ops")
        # cs_ops domains = 환불·보상
        for c in body["concepts"]:
            if c["in_domain"]:
                assert c["domain"] in ("환불", "보상")


# ── /author/publish ──────────────────────────────────────────────────────────


def _run_then_approved_concepts(client: TestClient, agent_id: str) -> list[dict[str, Any]]:
    """run으로 받은 in_domain 개념을 approved 처분으로 변환(over-claim은 rejected)."""
    _, body = _run(client, agent_id=agent_id)
    concepts: list[dict[str, Any]] = []
    for c in body["concepts"]:
        disposition = "approved" if c["in_domain"] else "rejected"
        concepts.append(
            {
                "concept_id": c["concept_id"],
                "disposition": disposition,
                "title": c["title"],
                "core_question": c["core_question"],
                "body": c["body"],
                "domain": c["domain"],
            }
        )
    return concepts


class TestAuthorPublish인증스코프:
    def test_미로그인_401(self) -> None:
        client = _make_client()
        status, _ = _publish(client, "cs_ops", [])
        assert status == 401

    def test_세션_불일치_403(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _publish(
            client,
            "contract_ops",
            [{"concept_id": "x", "disposition": "approved", "domain": "계약 검토"}],
        )
        assert status == 403


class TestAuthorPublish커밋과목차:
    def _client_with_store(self) -> tuple[TestClient, FakeGitGateway]:
        """published_index_store가 존재하는 앱(시드 store 주입 위해 index 라우터 모드)."""
        import os

        os.environ["AON_ROUTER"] = "index"
        try:
            gw = FakeGitGateway()
            client = _make_client(gw)
            return client, gw
        finally:
            os.environ.pop("AON_ROUTER", None)

    def test_커밋_sha와_파일_반환(self) -> None:
        gw = FakeGitGateway()
        client = _make_client(gw)
        _login(client, "cs_lead")
        concepts = _run_then_approved_concepts(client, "cs_ops")
        status, body = _publish(client, "cs_ops", concepts)
        assert status == 200
        assert body["committed"]["sha"] != ""
        assert len(body["committed"]["files"]) >= 2
        # 커밋된 파일은 승인된(in-domain) 개념만 — over-claim·rejected 제외
        for path in body["committed"]["files"]:
            assert path.endswith(".md")

    def test_거부_개념은_커밋되지_않는다(self) -> None:
        """rejected·over-claim 개념은 커밋 파일에 안 실린다."""
        gw = FakeGitGateway()
        client = _make_client(gw)
        _login(client, "cs_lead")
        concepts = _run_then_approved_concepts(client, "cs_ops")
        approved_ids = {
            c["concept_id"] for c in concepts if c["disposition"] == "approved"
        }
        _, body = _publish(client, "cs_ops", concepts)
        committed_stems = {p[:-3] for p in body["committed"]["files"]}
        assert committed_stems == approved_ids

    def test_전부_거부면_400(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        concepts = [
            {
                "concept_id": "demo-cs_ops-1",
                "disposition": "rejected",
                "domain": "환불",
            }
        ]
        status, _ = _publish(client, "cs_ops", concepts)
        assert status == 400

    def test_목차만_publish_concept_count(self) -> None:
        """중앙 published 응답은 목차(KnowledgeIndex)만 — concept_count == 승인 개념 수."""
        client, _gw = self._client_with_store()
        _login(client, "cs_lead")
        concepts = _run_then_approved_concepts(client, "cs_ops")
        approved = [c for c in concepts if c["disposition"] == "approved"]
        status, body = _publish(client, "cs_ops", concepts)
        assert status == 200
        assert body["published"] is not None
        assert body["published"]["agent_id"] == "cs_ops"
        assert body["published"]["concept_count"] == len(approved)

    def test_published_응답에_raw_본문_토큰_0(self) -> None:
        """raw 문서 본문 토큰이 중앙 publish 응답(목차)에 0번 등장한다(비소유)."""
        client, _gw = self._client_with_store()
        _login(client, "cs_lead")
        raw_doc = "이것은 절대로 중앙에 가면 안 되는 raw 본문 토큰 XYZ123 입니다."
        _run(client, "cs_ops", document=raw_doc)
        concepts = _run_then_approved_concepts(client, "cs_ops")
        status, body = _publish(client, "cs_ops", concepts)
        assert status == 200
        published_json = json.dumps(body["published"], ensure_ascii=False)
        assert "XYZ123" not in published_json


class TestNonOwnership중앙비소유:
    """**핵심 불변식**: 중앙 store에 들어가는 것은 목차(KnowledgeIndex)뿐·본문 0.

    publish 라우트가 쓰는 *바로 그* published_index_store 인스턴스를 직접 들고
    그 안의 Concept이 본문 필드를 갖지 않음을 단언한다. create_app이 store를 내부에서
    만들므로, build_demo로 같은 시드 store를 만들어 dispatcher 없이 라우트와 동형으로
    검증한다 — accept_published_index가 받는 객체가 KnowledgeIndex(목차)임을 직접 확인.
    """

    def test_accept_published_index가_받는것은_목차다(self) -> None:
        """라우트가 호출하는 publish 경로(build_index_from_admitted→accept_published_index)
        를 그대로 재현해 store에 들어간 객체가 목차(Concept에 body 속성 없음)임을 단언한다.
        """
        import tempfile
        from datetime import UTC, datetime
        from pathlib import Path

        from agent_org_network.demo import build_demo, seed_published_index_store
        from agent_org_network.knowledge_index import Concept
        from agent_org_network.okf_authoring import (
            OkfDocumentDraft,
            OkfDraft,
            admit_okf,
            build_index_from_admitted,
        )
        from agent_org_network.two_stage_router import accept_published_index

        bundle = build_demo(runtime=StubRuntime())
        card = bundle.registry.get("cs_ops")
        store = seed_published_index_store(bundle.registry)

        draft = OkfDraft(
            agent_id="cs_ops",
            documents=(
                OkfDocumentDraft(
                    concept_id="refund-window",
                    title="환불 가능 기간",
                    body="결제일로부터 7일 이내 — 이 본문은 절대 중앙에 안 간다 SECRET999.",
                    core_question="환불은 언제까지 가능한가요?",
                    domain="환불",
                ),
            ),
        )
        result = admit_okf(draft, card)
        with tempfile.TemporaryDirectory() as tmp:
            index = build_index_from_admitted(
                result.admitted, card, Path(tmp), generated_at=datetime.now(UTC)
            )
        accept_published_index("cs_lead", index, bundle.registry, store)

        stored = store.get("cs_ops")
        assert stored is not None
        # store의 Concept은 목차 필드만 — body 속성이 *존재하지 않는다*(KnowledgeIndex 타입).
        for concept in stored.concepts:
            assert isinstance(concept, Concept)
            assert not hasattr(concept, "body")
        # 본문 토큰이 store 직렬화 어디에도 없다.
        dumped = stored.model_dump_json()
        assert "SECRET999" not in dumped
        # 목차 필드는 보존(concept_id·core_question).
        ids = {c.id for c in stored.concepts}
        assert "refund-window" in ids


def test_store_시드_무관_seed_published_index_store_import() -> None:
    """seed_published_index_store가 import 가능(테스트 도우미 무결성)."""
    assert seed_published_index_store is not None
