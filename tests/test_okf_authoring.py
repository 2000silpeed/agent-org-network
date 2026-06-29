"""OKF Authoring 값 객체 테스트 — T11.1·T11.2 (red→green→refactor).

RawSource·OkfDocumentDraft·OkfDraft의 frozen pydantic 값 객체 검증(T11.1).
OkfAuthor 포트·FakeAuthor·run_authoring_pipeline staged 오케스트레이터(T11.2).
SDK 0·IO 0·결정론. 실 LLM 호출 없음.
"""

from __future__ import annotations

import pytest

from agent_org_network.agent_card import is_safe_path_component
from agent_org_network.knowledge_index import ConceptEdge
from agent_org_network.okf_authoring import (
    AuthoredOkf,
    FakeAuthor,
    OkfAuthor,
    OkfDocumentDraft,
    OkfDraft,
    RawSource,
    run_authoring_pipeline,
)
from agent_org_network.worker import _is_safe_path_component  # pyright: ignore[reportPrivateUsage]


# ── (1) RawSource: 정상 생성 + frozen ─────────────────────────────────────────


def test_RawSource_정상_생성_및_frozen():
    src = RawSource(source_id="src-001", content="환불 정책 본문")
    assert src.source_id == "src-001"
    assert src.content == "환불 정책 본문"
    with pytest.raises(Exception):
        src.source_id = "changed"  # type: ignore[misc]


# ── (2) RawSource: 빈 source_id 거부 ─────────────────────────────────────────


def test_RawSource_빈_source_id_거부():
    with pytest.raises(Exception):
        RawSource(source_id="", content="내용")


# ── (3) RawSource: 빈 content 거부 ───────────────────────────────────────────


def test_RawSource_빈_content_거부():
    with pytest.raises(Exception):
        RawSource(source_id="src-001", content="")


# ── (4) OkfDocumentDraft: 전필드 정상 생성 ───────────────────────────────────


def test_OkfDocumentDraft_전필드_정상_생성():
    doc = OkfDocumentDraft(
        concept_id="refund-policy",
        title="환불 정책",
        body="7일 이내 환불 가능합니다.",
        core_question="환불이 가능한가요?",
        domain="환불",
        type="policy",
    )
    assert doc.concept_id == "refund-policy"
    assert doc.title == "환불 정책"
    assert doc.body == "7일 이내 환불 가능합니다."
    assert doc.core_question == "환불이 가능한가요?"
    assert doc.domain == "환불"
    assert doc.type == "policy"


# ── (5) OkfDocumentDraft: type=None 기본 ─────────────────────────────────────


def test_OkfDocumentDraft_type_None_기본():
    doc = OkfDocumentDraft(
        concept_id="pricing-v2",
        title="가격 정책",
        body="표준 요금제입니다.",
        core_question="가격은 얼마인가요?",
        domain="가격",
    )
    assert doc.type is None


# ── (6) OkfDocumentDraft: frozen ─────────────────────────────────────────────


def test_OkfDocumentDraft_frozen():
    doc = OkfDocumentDraft(
        concept_id="refund-policy",
        title="환불 정책",
        body="내용",
        core_question="질문",
        domain="환불",
    )
    with pytest.raises(Exception):
        doc.title = "바꾸기"  # type: ignore[misc]


# ── (7) concept_id sanitization: ../escape 거부 ──────────────────────────────


def test_OkfDocumentDraft_concept_id_상위경로_탈출_거부():
    with pytest.raises(Exception):
        OkfDocumentDraft(
            concept_id="../escape",
            title="제목",
            body="내용",
            core_question="질문",
            domain="도메인",
        )


# ── (8) concept_id sanitization: a/b·a\\b 거부 ───────────────────────────────


def test_OkfDocumentDraft_concept_id_슬래시_백슬래시_거부():
    with pytest.raises(Exception):
        OkfDocumentDraft(
            concept_id="a/b",
            title="제목",
            body="내용",
            core_question="질문",
            domain="도메인",
        )
    with pytest.raises(Exception):
        OkfDocumentDraft(
            concept_id="a\\b",
            title="제목",
            body="내용",
            core_question="질문",
            domain="도메인",
        )


# ── (9) concept_id sanitization: "."·".." 거부 ───────────────────────────────


def test_OkfDocumentDraft_concept_id_점_예약stem_거부():
    with pytest.raises(Exception):
        OkfDocumentDraft(
            concept_id=".",
            title="제목",
            body="내용",
            core_question="질문",
            domain="도메인",
        )
    with pytest.raises(Exception):
        OkfDocumentDraft(
            concept_id="..",
            title="제목",
            body="내용",
            core_question="질문",
            domain="도메인",
        )


# ── (10) concept_id sanitization: "/abs/path" 거부 ───────────────────────────


def test_OkfDocumentDraft_concept_id_절대경로_거부():
    with pytest.raises(Exception):
        OkfDocumentDraft(
            concept_id="/abs/path",
            title="제목",
            body="내용",
            core_question="질문",
            domain="도메인",
        )


# ── (11) concept_id sanitization: 빈/공백 거부 ───────────────────────────────


def test_OkfDocumentDraft_concept_id_빈_공백_거부():
    with pytest.raises(Exception):
        OkfDocumentDraft(
            concept_id="",
            title="제목",
            body="내용",
            core_question="질문",
            domain="도메인",
        )
    with pytest.raises(Exception):
        OkfDocumentDraft(
            concept_id="   ",
            title="제목",
            body="내용",
            core_question="질문",
            domain="도메인",
        )


# ── (12) concept_id sanitization: 정상 통과 ─────────────────────────────────


def test_OkfDocumentDraft_concept_id_정상_통과():
    doc1 = OkfDocumentDraft(
        concept_id="refund-policy",
        title="환불 정책",
        body="내용",
        core_question="질문",
        domain="환불",
    )
    assert doc1.concept_id == "refund-policy"

    doc2 = OkfDocumentDraft(
        concept_id="pricing_v2",
        title="가격 정책",
        body="내용",
        core_question="질문",
        domain="가격",
    )
    assert doc2.concept_id == "pricing_v2"


# ── (13) 빈 core_question 거부 ────────────────────────────────────────────────


def test_OkfDocumentDraft_빈_core_question_거부():
    with pytest.raises(Exception):
        OkfDocumentDraft(
            concept_id="refund-policy",
            title="제목",
            body="내용",
            core_question="",
            domain="환불",
        )


# ── (14) 빈 domain 거부 ───────────────────────────────────────────────────────


def test_OkfDocumentDraft_빈_domain_거부():
    with pytest.raises(Exception):
        OkfDocumentDraft(
            concept_id="refund-policy",
            title="제목",
            body="내용",
            core_question="질문",
            domain="",
        )


# ── (15) 빈 title 거부 ────────────────────────────────────────────────────────


def test_OkfDocumentDraft_빈_title_거부():
    with pytest.raises(Exception):
        OkfDocumentDraft(
            concept_id="refund-policy",
            title="",
            body="내용",
            core_question="질문",
            domain="환불",
        )


# ── (16) 빈 body 거부 ─────────────────────────────────────────────────────────


def test_OkfDocumentDraft_빈_body_거부():
    with pytest.raises(Exception):
        OkfDocumentDraft(
            concept_id="refund-policy",
            title="제목",
            body="",
            core_question="질문",
            domain="환불",
        )


# ── (17) card.domains에 없는 domain이어도 구성 성공 (권한 미검증 의도 고정) ──


def test_OkfDocumentDraft_card_domains에_없는_domain_구성_성공():
    """권한(domain∈card.domains) 검증은 값 객체 책임이 아니다(ADR 0004·형식만 검증)."""
    doc = OkfDocumentDraft(
        concept_id="refund-policy",
        title="환불 정책",
        body="내용",
        core_question="질문",
        domain="카드에없는도메인xyz",  # 임의 도메인 — 형식만 맞으면 통과
    )
    assert doc.domain == "카드에없는도메인xyz"


# ── (18) OkfDraft: 정상 생성 (edges 없음) ────────────────────────────────────


def test_OkfDraft_정상_생성_edges_없음():
    doc = OkfDocumentDraft(
        concept_id="refund-policy",
        title="환불 정책",
        body="내용",
        core_question="질문",
        domain="환불",
    )
    draft = OkfDraft(agent_id="cs-ops", documents=(doc,))
    assert draft.agent_id == "cs-ops"
    assert len(draft.documents) == 1
    assert draft.edges == ()


# ── (19) OkfDraft: 빈 documents=() 허용 ─────────────────────────────────────


def test_OkfDraft_빈_documents_허용():
    draft = OkfDraft(agent_id="cs-ops", documents=())
    assert draft.documents == ()


# ── (20) OkfDraft: 잘못된 agent_id 형식 거부 ─────────────────────────────────


def test_OkfDraft_잘못된_agent_id_거부():
    with pytest.raises(Exception):
        OkfDraft(agent_id="../evil", documents=())
    with pytest.raises(Exception):
        OkfDraft(agent_id="", documents=())
    with pytest.raises(Exception):
        OkfDraft(agent_id="has space", documents=())


# ── (21) OkfDraft: documents 중복 concept_id 거부 ───────────────────────────


def test_OkfDraft_중복_concept_id_거부():
    doc1 = OkfDocumentDraft(
        concept_id="refund-policy",
        title="환불 정책",
        body="내용",
        core_question="질문",
        domain="환불",
    )
    doc2 = OkfDocumentDraft(
        concept_id="refund-policy",  # 중복
        title="다른 제목",
        body="다른 내용",
        core_question="다른 질문",
        domain="환불",
    )
    with pytest.raises(Exception):
        OkfDraft(agent_id="cs-ops", documents=(doc1, doc2))


# ── (22) OkfDraft: knowledge_index.ConceptEdge 재사용 확인 ──────────────────


def test_OkfDraft_ConceptEdge_재사용():
    """knowledge_index.ConceptEdge를 새 타입 없이 그대로 재사용한다."""
    doc = OkfDocumentDraft(
        concept_id="refund-policy",
        title="환불 정책",
        body="내용",
        core_question="질문",
        domain="환불",
    )
    edge = ConceptEdge(from_id="refund-policy", to_id="pricing-v2", relation="related")
    draft = OkfDraft(agent_id="cs-ops", documents=(doc,), edges=(edge,))
    assert len(draft.edges) == 1
    assert isinstance(draft.edges[0], ConceptEdge)
    assert draft.edges[0].from_id == "refund-policy"


# ── (23) OkfDraft: frozen ────────────────────────────────────────────────────


def test_OkfDraft_frozen():
    draft = OkfDraft(agent_id="cs-ops", documents=())
    with pytest.raises(Exception):
        draft.agent_id = "changed"  # type: ignore[misc]


# ── (24) OkfDraft: dangling edge여도 구성 성공 (경계 고정) ──────────────────


def test_OkfDraft_dangling_edge_구성_성공():
    """documents에 없는 concept_id를 가리키는 edge여도 OkfDraft 구성은 성공한다.

    번들 닫힘 검증은 T11.3 책임. 값 객체는 dangling을 거부하지 않는다.
    """
    doc = OkfDocumentDraft(
        concept_id="refund-policy",
        title="환불 정책",
        body="내용",
        core_question="질문",
        domain="환불",
    )
    dangling_edge = ConceptEdge(
        from_id="refund-policy",
        to_id="nonexistent-concept",  # documents에 없는 concept
        relation="related",
    )
    draft = OkfDraft(agent_id="cs-ops", documents=(doc,), edges=(dangling_edge,))
    assert len(draft.edges) == 1


# ── (25) 정합: concept_id와 worker _is_safe_path_component 단일 권위 확인 ────


def test_concept_id_sanitization_단일_권위_정합():
    """OkfDocumentDraft concept_id 검증과 worker._is_safe_path_component가 동일 집합 통과.

    단일 권위: agent_card.is_safe_path_component를 통해 동일 화이트리스트를 공유한다.
    unsafe_cases는 is_safe_path_component(path-safety 화이트리스트)가 False인 값 —
    공백("   ")은 path-safety와 다른 범주이므로 이 케이스에서는 제외한다
    (공백은 테스트 11에서 OkfDocumentDraft 필드 검증으로 별도 거부 확인).
    """
    safe_cases = ["refund-policy", "pricing_v2", "concept123", "a"]
    # path-safety 화이트리스트가 False인 값(공백은 path-safety 범주 아님·제외)
    path_unsafe_cases = ["../escape", "a/b", "a\\b", ".", "..", "/abs/path", ""]

    for value in safe_cases:
        # worker._is_safe_path_component가 True인 값은 OkfDocumentDraft에서도 통과해야 한다
        assert _is_safe_path_component(value), f"{value!r}는 safe여야 함"
        assert is_safe_path_component(value), f"{value!r}는 공유 헬퍼에서도 safe여야 함"
        # OkfDocumentDraft 구성도 성공해야 한다
        doc = OkfDocumentDraft(
            concept_id=value,
            title="제목",
            body="내용",
            core_question="질문",
            domain="환불",
        )
        assert doc.concept_id == value

    for value in path_unsafe_cases:
        # worker._is_safe_path_component가 False인 값은 OkfDocumentDraft에서도 거부해야 한다
        assert not _is_safe_path_component(value), f"{value!r}는 unsafe여야 함"
        assert not is_safe_path_component(value), f"{value!r}는 공유 헬퍼에서도 unsafe여야 함"
        with pytest.raises(Exception):
            OkfDocumentDraft(
                concept_id=value,
                title="제목",
                body="내용",
                core_question="질문",
                domain="환불",
            )


# ── (26) 회귀: worker fetch path-sanitization 기존 동작 보존 ─────────────────


def test_worker_fetch_path_sanitization_회귀():
    """worker._is_safe_path_component가 agent_card.is_safe_path_component에 위임 후
    기존 동작(화이트리스트)을 그대로 유지한다.
    """
    safe = ["refund_policy", "pricing-v2", "concept1"]
    unsafe = ["../escape", "a/b", "a\\b", ".", "..", "/abs", ""]

    for value in safe:
        assert _is_safe_path_component(value), f"{value!r}는 safe여야 함(회귀)"
    for value in unsafe:
        assert not _is_safe_path_component(value), f"{value!r}는 unsafe여야 함(회귀)"


# ── T11.2: OkfAuthor 포트·FakeAuthor·run_authoring_pipeline ──────────────────


def _make_doc(concept_id: str) -> OkfDocumentDraft:
    return OkfDocumentDraft(
        concept_id=concept_id,
        title=f"제목-{concept_id}",
        body="본문 내용",
        core_question="핵심 질문?",
        domain="환불",
    )


def _make_sources() -> tuple[RawSource, ...]:
    return (
        RawSource(source_id="src-001", content="환불 정책 본문"),
        RawSource(source_id="src-002", content="가격 정책 본문"),
    )


def _make_fake_author() -> FakeAuthor:
    split_result = (_make_doc("refund-policy"), _make_doc("pricing-v2"))
    derive_result = (_make_doc("refund-policy"), _make_doc("pricing-v2"))
    edge = ConceptEdge(from_id="refund-policy", to_id="pricing-v2", relation="related")
    link_result = (edge,)
    return FakeAuthor(
        split_result=split_result,
        derive_result=derive_result,
        link_result=link_result,
    )


# ── (27) 포트 준수: FakeAuthor가 OkfAuthor 인자로 타입 통과 ─────────────────────


def test_FakeAuthor_OkfAuthor_포트_구조적_만족():
    """FakeAuthor가 OkfAuthor Protocol을 구조적으로 만족한다."""

    def _accept_author(author: OkfAuthor) -> None:
        pass

    fake = _make_fake_author()
    _accept_author(fake)  # 타입 통과 시 pyright가 green


# ── (28) 포트 준수: 각 단계 반환 타입 ─────────────────────────────────────────


def test_FakeAuthor_각_단계_반환_타입():
    """split·derive_core_questions·link 반환 타입이 tuple임을 확인한다."""
    fake = _make_fake_author()
    sources = _make_sources()

    split_ret = fake.split(sources)
    derive_ret = fake.derive_core_questions(split_ret)
    link_ret = fake.link(derive_ret)

    assert isinstance(split_ret, tuple)
    assert isinstance(derive_ret, tuple)
    assert isinstance(link_ret, tuple)
    assert all(isinstance(d, OkfDocumentDraft) for d in split_ret)
    assert all(isinstance(d, OkfDocumentDraft) for d in derive_ret)
    assert all(isinstance(e, ConceptEdge) for e in link_ret)


# ── (29) FakeAuthor 결정론: 같은 주입·여러 호출 같은 산출 ───────────────────


def test_FakeAuthor_결정론_여러_호출_같은_산출():
    """같은 주입으로 여러 번 호출해도 동일한 산출을 반환한다."""
    fake = _make_fake_author()
    sources = _make_sources()

    r1 = fake.split(sources)
    r2 = fake.split(sources)
    r3 = fake.split((_make_sources()[0],))  # 다른 입력도 같은 고정 산출

    assert r1 == r2 == r3


# ── (30) FakeAuthor 결정론: 주입 산출 그대로 반환(변형 0) ────────────────────


def test_FakeAuthor_주입_산출_그대로_반환():
    """split·derive_core_questions·link가 주입 시 고정 값을 변형 없이 반환한다."""
    split_result = (_make_doc("refund-policy"),)
    derive_result = (_make_doc("pricing-v2"),)
    edge = ConceptEdge(from_id="refund-policy", to_id="pricing-v2", relation="related")
    link_result = (edge,)

    fake = FakeAuthor(
        split_result=split_result,
        derive_result=derive_result,
        link_result=link_result,
    )
    sources = _make_sources()
    docs = (_make_doc("any"),)

    assert fake.split(sources) is split_result
    assert fake.derive_core_questions(docs) is derive_result
    assert fake.link(docs) is link_result


# ── (31) staged 순차: split_seen·derive_seen·link_seen 다 채움 ───────────────


def test_run_authoring_pipeline_staged_seen_모두_채움():
    """run_authoring_pipeline 실행 후 FakeAuthor.split_seen·derive_seen·link_seen이 모두 채워진다."""
    fake = _make_fake_author()
    sources = _make_sources()

    run_authoring_pipeline(agent_id="cs-ops", sources=sources, author=fake)

    assert fake.split_seen is not None
    assert fake.derive_seen is not None
    assert fake.link_seen is not None


# ── (32) staged 순차: 데이터 배선 — derive_seen==split_result·link_seen==derive_result ─


def test_run_authoring_pipeline_데이터_배선():
    """derive_seen이 split_result·link_seen이 derive_result와 동일한지 확인(staged 배선)."""
    split_result = (_make_doc("refund-policy"), _make_doc("pricing-v2"))
    derive_result = (_make_doc("refund-detail"),)
    edge = ConceptEdge(from_id="refund-detail", to_id="pricing-v2", relation="related")
    link_result = (edge,)

    fake = FakeAuthor(
        split_result=split_result,
        derive_result=derive_result,
        link_result=link_result,
    )
    sources = _make_sources()

    run_authoring_pipeline(agent_id="cs-ops", sources=sources, author=fake)

    # split 결과가 derive 입력으로 전달됐는지
    assert fake.derive_seen == split_result
    # derive 결과가 link 입력으로 전달됐는지
    assert fake.link_seen == derive_result


# ── (33) staged 순차: AuthoredOkf.draft 필드 = derive_result·edges = link_result ─


def test_run_authoring_pipeline_최종_AuthoredOkf_draft_필드():
    """AuthoredOkf.draft.documents가 derive_result·.edges가 link_result임을 확인한다."""
    split_result = (_make_doc("refund-policy"),)
    derive_result = (_make_doc("refund-detail"),)
    edge = ConceptEdge(from_id="refund-detail", to_id="pricing-v2", relation="related")
    link_result = (edge,)

    fake = FakeAuthor(
        split_result=split_result,
        derive_result=derive_result,
        link_result=link_result,
    )
    sources = _make_sources()

    result = run_authoring_pipeline(agent_id="cs-ops", sources=sources, author=fake)

    assert result.draft.documents == derive_result
    assert result.draft.edges == link_result


# ── (34) staged 순차: AuthoredOkf.sources == tuple(입력 sources) ─────────────


def test_run_authoring_pipeline_sources_보존():
    """AuthoredOkf.sources가 입력 sources를 tuple로 보존한다."""
    fake = _make_fake_author()
    sources = _make_sources()

    result = run_authoring_pipeline(agent_id="cs-ops", sources=sources, author=fake)

    assert result.sources == tuple(sources)


# ── (35) admission: 정상 조립 OkfDraft admission 통과 ────────────────────────


def test_run_authoring_pipeline_정상_OkfDraft_admission_통과():
    """run_authoring_pipeline이 정상 산출로 OkfDraft admission을 통과한다."""
    fake = _make_fake_author()
    sources = _make_sources()

    result = run_authoring_pipeline(agent_id="cs-ops", sources=sources, author=fake)

    assert isinstance(result, AuthoredOkf)
    assert isinstance(result.draft, OkfDraft)
    assert result.draft.agent_id == "cs-ops"


# ── (35b) admission: 중복 concept_id 주입 시 OkfDraft 생성에서 거부 ───────────


def test_run_authoring_pipeline_중복_concept_id_거부():
    """derive_result에 중복 concept_id가 있으면 run_authoring_pipeline이 예외를 던진다."""
    dup_doc = _make_doc("refund-policy")
    split_result = (dup_doc,)
    derive_result = (dup_doc, dup_doc)  # 중복 concept_id
    link_result: tuple[ConceptEdge, ...] = ()

    fake = FakeAuthor(
        split_result=split_result,
        derive_result=derive_result,
        link_result=link_result,
    )
    sources = _make_sources()

    with pytest.raises(Exception):
        run_authoring_pipeline(agent_id="cs-ops", sources=sources, author=fake)


# ── (36) seam 미선취: 반환형이 AuthoredOkf·KnowledgeIndex 필드 없음 ──────────


def test_AuthoredOkf_KnowledgeIndex_필드_없음():
    """AuthoredOkf에 KnowledgeIndex 관련 필드가 없음을 확인한다(5단계 미선취 가드)."""
    fake = _make_fake_author()
    sources = _make_sources()

    result = run_authoring_pipeline(agent_id="cs-ops", sources=sources, author=fake)

    assert not hasattr(result, "knowledge_index")
    assert not hasattr(result, "index")
    assert isinstance(result, AuthoredOkf)


# ── (37) seam 미선취: 오케스트레이터가 RawSource를 인자로 받음·파일/인제스트 호출 0 ─


def test_run_authoring_pipeline_RawSource_인자_수령_시그니처():
    """run_authoring_pipeline이 RawSource Sequence를 인자로 받는다(1단계 미선취 가드).

    파일/인제스트 호출이 없음을 시그니처로 확인 — sources는 이미 외부에서 제공된다.
    """
    import inspect

    sig = inspect.signature(run_authoring_pipeline)
    params = list(sig.parameters.keys())
    assert "sources" in params
    assert "agent_id" in params
    assert "author" in params
    # 파일 경로 인자가 없어야 한다(인제스트 미선취)
    assert "file_path" not in params
    assert "path" not in params


# ── (38) seam 미선취: HITL 상태 필드 없음 ─────────────────────────────────────


def test_AuthoredOkf_HITL_상태_필드_없음():
    """AuthoredOkf에 HITL 승인 상태 필드가 없음을 확인한다(T11.3 미선취 가드)."""
    fake = _make_fake_author()
    sources = _make_sources()

    result = run_authoring_pipeline(agent_id="cs-ops", sources=sources, author=fake)

    assert not hasattr(result, "approval_status")
    assert not hasattr(result, "hitl_status")
    assert not hasattr(result, "status")


# ── (39) one-shot 아님: 포트에 author_all 류 단일 메서드 없음 ────────────────


def test_OkfAuthor_포트_단계_메서드_3개만_존재():
    """OkfAuthor Protocol에 split·derive_core_questions·link 3개 메서드만 있고
    author_all·run·process 류 단일 메서드가 없음을 확인한다(one-shot 아님 가드).
    """
    import inspect

    members = {
        name
        for name, _ in inspect.getmembers(OkfAuthor)
        if not name.startswith("_")
    }
    assert "split" in members
    assert "derive_core_questions" in members
    assert "link" in members
    assert "author_all" not in members
    assert "run" not in members
    assert "process" not in members
