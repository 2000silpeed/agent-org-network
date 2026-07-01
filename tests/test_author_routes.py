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


# ── GET /author/index/{agent_id} — published 목차 조회 ────────────────────────


def _client_with_store() -> TestClient:
    """published_index_store가 존재하는 앱(index 라우터 모드 → store 시드)."""
    import os

    os.environ["AON_ROUTER"] = "index"
    try:
        return _make_client(FakeGitGateway())
    finally:
        os.environ.pop("AON_ROUTER", None)


def _get_index(client: TestClient, agent_id: str) -> tuple[int, Any]:
    http: Any = client
    res: Response = cast(Response, http.get(f"/author/index/{agent_id}"))
    try:
        body: Any = res.json()
    except Exception:
        body = res.text
    return res.status_code, body


class TestAuthorIndex목차조회:
    def test_미로그인_401(self) -> None:
        client = _make_client()
        status, _ = _get_index(client, "cs_ops")
        assert status == 401

    def test_타인_카드_403(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _get_index(client, "contract_ops")
        assert status == 403

    def test_미존재_카드_404(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _get_index(client, "no_such_card")
        assert status == 404

    def test_store_없으면_빈_concepts(self) -> None:
        """published_index_store 미존재(기본 라우터) → generated_at None·빈 배열(미아 아님)."""
        client = _make_client()  # 기본 라우터 — published_index_store None
        _login(client, "cs_lead")
        status, body = _get_index(client, "cs_ops")
        assert status == 200
        assert body["agent_id"] == "cs_ops"
        assert body["generated_at"] is None
        assert body["concepts"] == []

    def test_publish_후_목차_개념_반환(self) -> None:
        """publish 후 GET → 승인 개념이 목차 항목으로 돌아온다."""
        client = _client_with_store()
        _login(client, "cs_lead")
        concepts = _run_then_approved_concepts(client, "cs_ops")
        approved_ids = {
            c["concept_id"] for c in concepts if c["disposition"] == "approved"
        }
        status, body = _publish(client, "cs_ops", concepts)
        assert status == 200

        status, body = _get_index(client, "cs_ops")
        assert status == 200
        assert body["agent_id"] == "cs_ops"
        assert body["generated_at"] is not None
        returned_ids = {c["id"] for c in body["concepts"]}
        assert returned_ids == approved_ids
        # 목차 항목 필드 — 본문(body) 없음.
        for c in body["concepts"]:
            assert set(c.keys()) == {"id", "label", "core_question", "domain", "type"}

    def test_목차에_raw_본문_토큰_0(self) -> None:
        """raw 본문 토큰이 GET 목차 응답에 0번 등장한다(비소유)."""
        client = _client_with_store()
        _login(client, "cs_lead")
        _run(client, "cs_ops", document="중앙에 가면 안 되는 본문 토큰 ZZZ987 입니다.")
        concepts = _run_then_approved_concepts(client, "cs_ops")
        _publish(client, "cs_ops", concepts)
        _, body = _get_index(client, "cs_ops")
        assert "ZZZ987" not in json.dumps(body, ensure_ascii=False)


def test_store_시드_무관_seed_published_index_store_import() -> None:
    """seed_published_index_store가 import 가능(테스트 도우미 무결성)."""
    assert seed_published_index_store is not None


# ── T11.8a — 멱등·증분 publish (ADR 0032 결정 B1) ────────────────────────────


def _client_with_store_and_gw() -> tuple[TestClient, FakeGitGateway]:
    """published_index_store + FakeGitGateway가 있는 앱 — 멱등·증분 테스트용."""
    import os

    os.environ["AON_ROUTER"] = "index"
    try:
        gw = FakeGitGateway()
        client = _make_client(gw)
        return client, gw
    finally:
        os.environ.pop("AON_ROUTER", None)


def _single_concept(
    concept_id: str,
    title: str,
    domain: str = "환불",
    core_question: str | None = None,
    body: str = "본문입니다.",
) -> dict[str, Any]:
    """테스트용 단일 approved 개념 dict."""
    return {
        "concept_id": concept_id,
        "disposition": "approved",
        "title": title,
        "domain": domain,
        "core_question": core_question or title,
        "body": body,
    }


class TestPublish증분누적_T11_8a:
    """ADR 0032 결정 B1 — 두 번째 publish가 첫 번째를 덮지 않는다(증분 누적)."""

    def test_두_문서_연속_publish_후_인덱스에_둘_다_존재(self) -> None:
        """문서 A publish(개념 a1) → 문서 B publish(개념 b1) → 인덱스에 a1·b1 둘 다 있어야 한다.

        현재 코드(이번 admitted만 임시 디렉터리에서 도출)면 b1만 남아 red.
        ADR 0032 B1 수정 후 green.
        """
        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")

        # 첫 번째 publish: 개념 a1
        status_a, body_a = _publish(
            client, "cs_ops", [_single_concept("refund-window-a1", "환불 가능 기간 A")]
        )
        assert status_a == 200, f"첫 번째 publish 실패: {body_a}"

        # 두 번째 publish: 개념 b1
        status_b, body_b = _publish(
            client, "cs_ops", [_single_concept("refund-window-b1", "환불 가능 기간 B")]
        )
        assert status_b == 200, f"두 번째 publish 실패: {body_b}"

        # 인덱스 조회 — a1·b1 둘 다 있어야 한다(증분 누적)
        status_idx, body_idx = _get_index(client, "cs_ops")
        assert status_idx == 200
        concept_ids = {c["id"] for c in body_idx["concepts"]}
        assert "refund-window-a1" in concept_ids, (
            f"첫 번째 개념이 인덱스에 없다(두 번째 publish가 첫 번째를 지움): {concept_ids}"
        )
        assert "refund-window-b1" in concept_ids, (
            f"두 번째 개념이 인덱스에 없다: {concept_ids}"
        )

    def test_같은_concept_id_재게시는_중복_없이_덮어쓰기(self) -> None:
        """같은 concept_id를 두 번 publish → 인덱스에 1건(멱등 덮어쓰기·중복 없음).

        ADR 0032 B1: 같은 concept_id → 같은 파일 경로 → git이 덮어씀(파일 단위 멱등).
        """
        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")

        concept = _single_concept("refund-window-idem", "환불 기간 멱등 테스트")

        _publish(client, "cs_ops", [concept])
        _publish(client, "cs_ops", [concept])

        status_idx, body_idx = _get_index(client, "cs_ops")
        assert status_idx == 200
        ids = [c["id"] for c in body_idx["concepts"]]
        idem_ids = [i for i in ids if i == "refund-window-idem"]
        assert len(idem_ids) == 1, f"같은 concept_id가 중복됨: {ids}"

    def test_단일_publish_기존_동작_유지(self) -> None:
        """기존 단일 publish 회귀 — 한 번 publish 후 인덱스에 해당 개념 존재."""
        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")

        status, _ = _publish(
            client, "cs_ops", [_single_concept("refund-regression", "회귀 테스트 개념")]
        )
        assert status == 200

        status_idx, body_idx = _get_index(client, "cs_ops")
        assert status_idx == 200
        concept_ids = {c["id"] for c in body_idx["concepts"]}
        assert "refund-regression" in concept_ids


# ── GET/PUT/DELETE /author/concept/{agent_id}/{concept_id} — 상세 조회·편집·삭제 ─
# (ADR 0032 OQ-3 — owner 자기 게시 개념 상세 조회·편집·삭제)


def _get_concept(
    client: TestClient, agent_id: str, concept_id: str
) -> tuple[int, Any]:
    http: Any = client
    res: Response = cast(
        Response, http.get(f"/author/concept/{agent_id}/{concept_id}")
    )
    try:
        body: Any = res.json()
    except Exception:
        body = res.text
    return res.status_code, body


def _put_concept(
    client: TestClient, agent_id: str, concept_id: str, payload: dict[str, Any]
) -> tuple[int, Any]:
    http: Any = client
    res: Response = cast(
        Response,
        http.put(f"/author/concept/{agent_id}/{concept_id}", json=payload),
    )
    try:
        body: Any = res.json()
    except Exception:
        body = res.text
    return res.status_code, body


def _delete_concept(
    client: TestClient, agent_id: str, concept_id: str
) -> tuple[int, Any]:
    http: Any = client
    res: Response = cast(
        Response, http.delete(f"/author/concept/{agent_id}/{concept_id}")
    )
    try:
        body: Any = res.json()
    except Exception:
        body = res.text
    return res.status_code, body


class TestGetConcept상세조회:
    """GET /author/concept/{agent_id}/{concept_id} — 본문 포함 owner 자기 조회."""

    def test_미로그인_401(self) -> None:
        client = _make_client()
        status, _ = _get_concept(client, "cs_ops", "refund-window")
        assert status == 401

    def test_타인_카드_403(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _get_concept(client, "contract_ops", "x")
        assert status == 403

    def test_미존재_카드_404(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _get_concept(client, "no_such_card", "x")
        assert status == 404

    def test_publish_후_본문_포함_조회(self) -> None:
        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")
        _publish(
            client,
            "cs_ops",
            [
                _single_concept(
                    "refund-detail",
                    "환불 상세",
                    core_question="환불은 언제까지?",
                    body="결제일로부터 7일 이내 환불 가능합니다.",
                )
            ],
        )
        status, body = _get_concept(client, "cs_ops", "refund-detail")
        assert status == 200
        assert body["concept_id"] == "refund-detail"
        assert body["title"] == "환불 상세"
        assert body["core_question"] == "환불은 언제까지?"
        assert body["domain"] == "환불"
        assert "7일 이내" in body["body"]

    def test_미존재_개념_404(self) -> None:
        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")
        _publish(client, "cs_ops", [_single_concept("exists", "있음")])
        status, _ = _get_concept(client, "cs_ops", "does-not-exist")
        assert status == 404

    def test_traversal_concept_id_거부(self) -> None:
        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")
        # 경로 구분자가 든 concept_id는 라우트 매칭 단계나 검증에서 막힌다(200 아님).
        status, _ = _get_concept(client, "cs_ops", "..%2f..%2fetc%2fpasswd")
        assert status != 200


class TestPutConcept편집:
    """PUT /author/concept/{agent_id}/{concept_id} — 편집(덮어쓰기·미지정 보존)."""

    def test_미로그인_401(self) -> None:
        client = _make_client()
        status, _ = _put_concept(client, "cs_ops", "x", {"title": "변경"})
        assert status == 401

    def test_타인_카드_403(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _put_concept(client, "contract_ops", "x", {"title": "변경"})
        assert status == 403

    def test_편집이_인덱스_재도출에_반영(self) -> None:
        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")
        _publish(
            client,
            "cs_ops",
            [
                _single_concept(
                    "refund-edit",
                    "환불 초기 제목",
                    core_question="초기 질문?",
                    body="초기 본문",
                )
            ],
        )
        status, _ = _put_concept(
            client,
            "cs_ops",
            "refund-edit",
            {"core_question": "수정된 질문?", "body": "수정된 본문"},
        )
        assert status == 200

        # 상세 조회 — 변경 반영, 미지정 title은 보존
        status_g, body_g = _get_concept(client, "cs_ops", "refund-edit")
        assert status_g == 200
        assert body_g["core_question"] == "수정된 질문?"
        assert body_g["body"] == "수정된 본문"
        assert body_g["title"] == "환불 초기 제목"  # 미지정 보존

        # 중앙 목차에도 반영(core_question 변경)
        _, body_idx = _get_index(client, "cs_ops")
        concept = next(c for c in body_idx["concepts"] if c["id"] == "refund-edit")
        assert "수정된 질문" in concept["core_question"]

    def test_overclaim_domain_400(self) -> None:
        """편집으로 권한 밖 domain을 주면 admit_okf가 떨궈 400."""
        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")
        _publish(client, "cs_ops", [_single_concept("refund-oc", "환불 개념")])
        status, _ = _put_concept(
            client, "cs_ops", "refund-oc", {"domain": "계약 검토"}
        )
        assert status == 400

    def test_편집_미존재_개념_404(self) -> None:
        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")
        _publish(client, "cs_ops", [_single_concept("exists2", "있음2")])
        status, _ = _put_concept(
            client, "cs_ops", "ghost", {"title": "유령"}
        )
        assert status == 404

    def test_편집_후_concept_count_보존(self) -> None:
        """편집은 개념 수를 바꾸지 않는다(덮어쓰기)."""
        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")
        _publish(
            client,
            "cs_ops",
            [
                _single_concept("ca", "개념 A"),
                _single_concept("cb", "개념 B"),
            ],
        )
        status, body = _put_concept(client, "cs_ops", "ca", {"title": "개념 A 수정"})
        assert status == 200
        assert body["published"]["concept_count"] == 2


class TestDeleteConcept삭제:
    """DELETE /author/concept/{agent_id}/{concept_id} — 삭제(인덱스에서 빠짐)."""

    def test_미로그인_401(self) -> None:
        client = _make_client()
        status, _ = _delete_concept(client, "cs_ops", "x")
        assert status == 401

    def test_타인_카드_403(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _delete_concept(client, "contract_ops", "x")
        assert status == 403

    def test_삭제되면_인덱스에서_빠진다(self) -> None:
        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")
        _publish(
            client,
            "cs_ops",
            [
                _single_concept("keep-me", "남길 개념"),
                _single_concept("del-me", "삭제할 개념"),
            ],
        )
        status, _ = _delete_concept(client, "cs_ops", "del-me")
        assert status == 200

        _, body_idx = _get_index(client, "cs_ops")
        ids = {c["id"] for c in body_idx["concepts"]}
        assert "del-me" not in ids
        assert "keep-me" in ids  # 다른 개념 보존

    def test_삭제_후_남은_concept_count(self) -> None:
        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")
        _publish(
            client,
            "cs_ops",
            [
                _single_concept("d1", "개념1"),
                _single_concept("d2", "개념2"),
                _single_concept("d3", "개념3"),
            ],
        )
        status, body = _delete_concept(client, "cs_ops", "d2")
        assert status == 200
        assert body["published"]["concept_count"] == 2

    def test_마지막_개념_삭제하면_빈_인덱스(self) -> None:
        """마지막 개념 삭제 → 빈 concepts(0 후보·미아 없음 보존)."""
        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")
        _publish(client, "cs_ops", [_single_concept("only-one", "유일 개념")])
        status, body = _delete_concept(client, "cs_ops", "only-one")
        assert status == 200
        assert body["published"]["concept_count"] == 0

        _, body_idx = _get_index(client, "cs_ops")
        assert body_idx["concepts"] == []

    def test_삭제된_개념_상세조회_404(self) -> None:
        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")
        _publish(
            client,
            "cs_ops",
            [_single_concept("gone", "사라질 개념"), _single_concept("stay", "남을 개념")],
        )
        _delete_concept(client, "cs_ops", "gone")
        status, _ = _get_concept(client, "cs_ops", "gone")
        assert status == 404


# ── POST /author/dedup/{agent_id} — near-dup 후보 탐지(ADR 0032 결정 C·탐지 전용) ──
# `select_embedder`를 모킹해 결정론 보장(실 fastembed 모델은 게이트 밖·여기서 로드 안 함).


def _dedup(
    client: TestClient, agent_id: str, concepts: list[dict[str, Any]]
) -> tuple[int, Any]:
    http: Any = client
    res: Response = cast(
        Response,
        http.post(f"/author/dedup/{agent_id}", json={"concepts": concepts}),
    )
    try:
        body: Any = res.json()
    except Exception:
        body = res.text
    return res.status_code, body


def _dedup_concept(
    concept_id: str,
    title: str,
    core_question: str,
    body: str,
    domain: str = "환불",
) -> dict[str, Any]:
    """POST /author/dedup 요청의 신규 staged 개념 1건."""
    return {
        "concept_id": concept_id,
        "title": title,
        "core_question": core_question,
        "body": body,
        "domain": domain,
    }


def _embed_text(title: str, core_question: str, body: str) -> str:
    """라우트의 임베딩 입력 합성과 동일(title\\ncore_question\\nbody)."""
    return f"{title}\n{core_question}\n{body}"


class TestAuthorDedup후보탐지:
    """POST /author/dedup/{agent_id} — near-dup 후보 탐지(읽기 전용·중앙/owner git 무쓰기)."""

    def test_미로그인_401(self) -> None:
        client = _make_client()
        status, _ = _dedup(
            client, "cs_ops", [_dedup_concept("x", "제목", "질문?", "본문")]
        )
        assert status == 401

    def test_타인_카드_403(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _dedup(
            client, "contract_ops", [_dedup_concept("x", "제목", "질문?", "본문")]
        )
        assert status == 403

    def test_미존재_카드_404(self) -> None:
        client = _make_client()
        _login(client, "cs_lead")
        status, _ = _dedup(
            client, "no_such_card", [_dedup_concept("x", "제목", "질문?", "본문")]
        )
        assert status == 404

    def test_임베더_None이면_빈_candidates(self) -> None:
        """운영 기본(select_embedder가 None=비활성) → 임베딩 스킵·빈 후보(미아 아님)."""
        import agent_org_network.web as web_mod

        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")
        orig = web_mod.select_embedder
        try:
            web_mod.select_embedder = lambda: None  # type: ignore[assignment]
            status, body = _dedup(
                client, "cs_ops", [_dedup_concept("n1", "환불 정책", "환불은?", "본문")]
            )
        finally:
            web_mod.select_embedder = orig  # type: ignore[assignment]
        assert status == 200
        assert body["candidates"] == []

    def test_게시_라이브러리_없으면_빈_candidates(self) -> None:
        """게시 개념 0(번들 없음) → existing 0 → 후보 0(임베더 활성이어도 빈 후보)."""
        import agent_org_network.web as web_mod
        from agent_org_network.okf_dedup import FakeEmbedder

        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")
        new_text = _embed_text("환불 정책", "환불은 언제까지?", "7일 이내")
        fake = FakeEmbedder({new_text: (1.0, 0.0, 0.0)})
        orig = web_mod.select_embedder
        try:
            web_mod.select_embedder = lambda: fake  # type: ignore[assignment]
            status, body = _dedup(
                client,
                "cs_ops",
                [_dedup_concept("n1", "환불 정책", "환불은 언제까지?", "7일 이내")],
            )
        finally:
            web_mod.select_embedder = orig  # type: ignore[assignment]
        assert status == 200
        assert body["candidates"] == []

    def test_정상_후보_auto_suggest_와_similar(self) -> None:
        """게시 개념 2건 + 신규 1건, Fake 임베더 주입으로 알려진 cosine → 등급 분류 검증.

        new(v=[1,0,0])와:
          - existing dup(v=[1,0,0]) → cosine 1.0 ≥ 0.88 → auto_suggest
          - existing similar(v=[0.8,0.6,0]) → cosine 0.8 ∈ [0.70,0.88) → similar
        정규화 벡터라 dot=cosine. 게시 개념의 합성 텍스트는 게시 후 실제 저장값으로 구성한다.
        """
        import agent_org_network.web as web_mod
        from agent_org_network.okf_dedup import FakeEmbedder

        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")

        # 게시 라이브러리에 2건 게시(dup·similar 대상)
        _publish(
            client,
            "cs_ops",
            [
                _single_concept(
                    "existing-dup",
                    "환불 가능 기간",
                    core_question="환불은 언제까지 가능한가요?",
                    body="구매일로부터 7일 이내 환불 가능합니다.",
                ),
                _single_concept(
                    "existing-similar",
                    "교환 안내",
                    core_question="교환은 어떻게 하나요?",
                    body="상품 교환은 영업일 기준 3일 내 처리됩니다.",
                ),
            ],
        )

        # 게시 후 실제 저장된 값으로 existing 합성 텍스트 구성(render→parse 왕복 반영).
        _, dup_doc = _get_concept(client, "cs_ops", "existing-dup")
        _, sim_doc = _get_concept(client, "cs_ops", "existing-similar")
        dup_text = _embed_text(
            dup_doc["title"], dup_doc["core_question"], dup_doc["body"]
        )
        sim_text = _embed_text(
            sim_doc["title"], sim_doc["core_question"], sim_doc["body"]
        )

        new_title, new_q, new_body = "환불 기간 정리", "환불 기한이 어떻게 되나요?", "환불은 7일 이내."
        new_text = _embed_text(new_title, new_q, new_body)

        fake = FakeEmbedder(
            {
                new_text: (1.0, 0.0, 0.0),
                dup_text: (1.0, 0.0, 0.0),  # cosine 1.0 → auto_suggest
                sim_text: (0.8, 0.6, 0.0),  # cosine 0.8 → similar
            }
        )
        orig = web_mod.select_embedder
        try:
            web_mod.select_embedder = lambda: fake  # type: ignore[assignment]
            status, body = _dedup(
                client,
                "cs_ops",
                [_dedup_concept("new-1", new_title, new_q, new_body)],
            )
        finally:
            web_mod.select_embedder = orig  # type: ignore[assignment]

        assert status == 200, body
        cands = body["candidates"]
        assert len(cands) == 2
        # similarity 내림차순 정렬 — auto_suggest(1.0) 먼저
        assert cands[0]["existing_concept_id"] == "existing-dup"
        assert cands[0]["grade"] == "auto_suggest"
        assert abs(cands[0]["similarity"] - 1.0) < 1e-9
        assert cands[1]["existing_concept_id"] == "existing-similar"
        assert cands[1]["grade"] == "similar"
        assert abs(cands[1]["similarity"] - 0.8) < 1e-9
        # 모든 후보의 new_concept_id는 요청 개념
        assert all(c["new_concept_id"] == "new-1" for c in cands)

    def test_번들_메타_index_md는_비교_대상에서_제외된다(self) -> None:
        """`index.md`(type=index·번들 메타)는 개념이 아니므로 dedup 비교에서 빠진다.

        디스크 시드 번들(seed_gateway_from_disk)엔 index.md가 흔히 같이 있다 — 게이트 내
        FakeGitGateway 테스트는 보통 publish 경로만 거쳐 index.md가 안 생기므로(이 버그가
        가게 내 테스트로는 안 잡혔다), 여기서 index.md를 직접 커밋해 재현·고정한다.
        """
        import agent_org_network.web as web_mod
        from agent_org_network.git_gateway import CommitRequest, OkfFile
        from agent_org_network.okf_dedup import FakeEmbedder

        client, gw = _client_with_store_and_gw()
        _login(client, "cs_lead")
        _publish(client, "cs_ops", [_single_concept("real", "진짜 개념")])

        # 번들 메타 index.md를 직접 커밋(디스크 시드가 만드는 모양 재현).
        gw.commit_bundle(
            CommitRequest(
                agent_id="cs_ops",
                author="cs_lead",
                files=(
                    OkfFile(
                        path="index.md",
                        content=(
                            "---\ntitle: cs_ops 지식 번들\ndescription: 목차\ntags:\n"
                            "- 환불\ntype: index\n---\n\n번들 메타.\n"
                        ),
                    ),
                ),
                message="번들 메타 커밋",
            )
        )

        _, real_doc = _get_concept(client, "cs_ops", "real")
        real_text = _embed_text(real_doc["title"], real_doc["core_question"], real_doc["body"])
        new_text = _embed_text("신규 개념", "신규 질문?", "신규 본문")
        fake = FakeEmbedder({new_text: (1.0, 0.0, 0.0), real_text: (0.0, 1.0, 0.0)})
        orig = web_mod.select_embedder
        try:
            web_mod.select_embedder = lambda: fake  # type: ignore[assignment]
            status, body = _dedup(
                client, "cs_ops", [_dedup_concept("n1", "신규 개념", "신규 질문?", "신규 본문")]
            )
        finally:
            web_mod.select_embedder = orig  # type: ignore[assignment]

        assert status == 200, body
        existing_ids = {c["existing_concept_id"] for c in body["candidates"]}
        assert "index" not in existing_ids

    def test_탐지는_중앙_목차를_안_바꾼다(self) -> None:
        """dedup은 읽기 전용 — 호출 전후 published 목차(concept_count) 불변."""
        import agent_org_network.web as web_mod
        from agent_org_network.okf_dedup import FakeEmbedder

        client, _gw = _client_with_store_and_gw()
        _login(client, "cs_lead")
        _publish(client, "cs_ops", [_single_concept("keep", "남는 개념")])

        _, before = _get_index(client, "cs_ops")
        count_before = len(before["concepts"])

        _, kept = _get_concept(client, "cs_ops", "keep")
        kept_text = _embed_text(
            kept["title"], kept["core_question"], kept["body"]
        )
        new_text = _embed_text("새 개념", "새 질문?", "새 본문")
        fake = FakeEmbedder(
            {new_text: (1.0, 0.0, 0.0), kept_text: (0.0, 1.0, 0.0)}
        )
        orig = web_mod.select_embedder
        try:
            web_mod.select_embedder = lambda: fake  # type: ignore[assignment]
            status, _ = _dedup(
                client, "cs_ops", [_dedup_concept("new-x", "새 개념", "새 질문?", "새 본문")]
            )
        finally:
            web_mod.select_embedder = orig  # type: ignore[assignment]
        assert status == 200

        _, after = _get_index(client, "cs_ops")
        assert len(after["concepts"]) == count_before
