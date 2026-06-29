"""OKF Authoring 값 객체 테스트 — T11.1·T11.2·T11.3 (red→green→refactor).

RawSource·OkfDocumentDraft·OkfDraft의 frozen pydantic 값 객체 검증(T11.1).
OkfAuthor 포트·FakeAuthor·run_authoring_pipeline staged 오케스트레이터(T11.2).
HITL 상태기계·OKF admission 검증·마크다운 직렬화·commit seam(T11.3).
SDK 0·IO 0·결정론. 실 LLM 호출 없음.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from agent_org_network.agent_card import AgentCard, is_safe_path_component
from agent_org_network.knowledge_index import ConceptEdge
from agent_org_network.okf_authoring import (
    AdmissionResult,
    Approved,
    AuthoredOkf,
    Edited,
    FakeAuthor,
    OkfAuthor,
    OkfDocumentDraft,
    OkfDraft,
    Rejected,
    StageReview,
    RawSource,
    admit_okf,
    prepare_commit_request,
    render_okf_markdown,
    run_authoring_pipeline,
    set_disposition,
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


# ── T11.3 헬퍼 ───────────────────────────────────────────────────────────────


def _make_card(
    agent_id: str = "cs-ops",
    domains: list[str] | None = None,
    cannot_answer: list[str] | None = None,
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner="sungwoon",
        team="cs",
        summary="고객 지원 에이전트",
        domains=domains if domains is not None else ["환불", "배송"],
        last_reviewed_at=date(2026, 1, 1),
        cannot_answer=cannot_answer if cannot_answer is not None else [],
    )


def _make_split_doc(concept_id: str, domain: str = "환불") -> OkfDocumentDraft:
    return OkfDocumentDraft(
        concept_id=concept_id,
        title=f"제목-{concept_id}",
        body="본문 내용입니다.",
        core_question=f"{concept_id} 핵심 질문?",
        domain=domain,
    )


def _make_stage_review_split(
    concept_id: str = "refund-policy",
    domain: str = "환불",
) -> StageReview:
    return StageReview(
        stage="split",
        documents=(
            _make_split_doc(concept_id, domain),
        ),
    )


# ── T11.3 전이 정상 ───────────────────────────────────────────────────────────


def test_T11_3_staged_to_Approved_전이():
    """(1) staged(None) → Approved 정상 전이"""
    review = _make_stage_review_split()
    result = set_disposition(review, Approved())
    assert isinstance(result.disposition, Approved)
    assert result.stage == review.stage
    assert result.documents == review.documents


def test_T11_3_staged_to_Edited_교체본_다음_입력():
    """(2) staged → Edited(교체본) — documents가 교체본으로 교체됨"""
    original_doc = _make_split_doc("refund-policy")
    review = StageReview(stage="split", documents=(original_doc,))

    replacement = _make_split_doc("refund-policy-v2")
    result = set_disposition(review, Edited(documents=(replacement,)))

    assert isinstance(result.disposition, Edited)
    assert result.disposition.documents == (replacement,)


def test_T11_3_staged_to_Rejected_종착():
    """(3) staged → Rejected 종착 — reason 보존"""
    review = _make_stage_review_split()
    result = set_disposition(review, Rejected(reason="부적절한 내용"))
    assert isinstance(result.disposition, Rejected)
    assert result.disposition.reason == "부적절한 내용"


def test_T11_3_Edited_교체본_값객체_검증_빈_core_question_거부():
    """(4) Edited 교체본도 T11.1 admission 걸림 — 빈 core_question 거부"""
    with pytest.raises(Exception):
        Edited(
            documents=(
                OkfDocumentDraft(
                    concept_id="refund-policy",
                    title="제목",
                    body="내용",
                    core_question="",  # 빈 core_question
                    domain="환불",
                ),
            )
        )


# ── T11.3 금지 전이 ───────────────────────────────────────────────────────────


def test_T11_3_처분된_것_재처분_거부():
    """(5) 이미 처분된 StageReview를 재처분하면 ValueError"""
    review = _make_stage_review_split()
    already_disposed = set_disposition(review, Approved())
    with pytest.raises(ValueError, match="이미 처분"):
        set_disposition(already_disposed, Rejected(reason="재처분 시도"))


def test_T11_3_단계_payload_불일치_거부():
    """(6) split 단계인데 Edited.edges만 채우면 ValueError(단계×payload 불일치)"""
    edge = ConceptEdge(from_id="refund-policy", to_id="pricing-v2", relation="related")
    review = StageReview(stage="split", documents=(_make_split_doc("refund-policy"),))
    with pytest.raises(ValueError, match="단계.*불일치|payload.*불일치|split.*edges"):
        set_disposition(review, Edited(edges=(edge,)))


def test_T11_3_link_단계_Edited_documents만_채우면_거부():
    """(6b) link 단계인데 Edited.documents만 채우면 ValueError"""
    review = StageReview(
        stage="link",
        edges=(ConceptEdge(from_id="a", to_id="b", relation="related"),),
    )
    with pytest.raises(ValueError, match="단계.*불일치|payload.*불일치|link.*documents"):
        set_disposition(review, Edited(documents=(_make_split_doc("a"),)))


def test_T11_3_staged는_다음_단계_제외():
    """(7) disposition=None(staged)인 review는 승인분 추림에서 제외"""
    staged_review = StageReview(stage="split", documents=(_make_split_doc("refund-policy"),))
    # staged는 approved_documents에 안 나와야 함
    docs = _collect_approved_documents([staged_review])
    assert docs == ()


# ── T11.3 승인분만 ────────────────────────────────────────────────────────────


def _collect_approved_documents(
    reviews: list[StageReview],
) -> tuple[OkfDocumentDraft, ...]:
    """StageReview 리스트에서 승인된 documents만 수집하는 헬퍼."""
    result: list[OkfDocumentDraft] = []
    for r in reviews:
        if isinstance(r.disposition, Approved):
            result.extend(r.documents)
        elif isinstance(r.disposition, Edited):
            result.extend(r.disposition.documents)
    return tuple(result)


def test_T11_3_일부_Approved_일부_Rejected_승인분만():
    """(8) 일부 Approved·일부 Rejected → 다음 입력에 승인분만"""
    doc_a = _make_split_doc("refund-policy")
    doc_b = _make_split_doc("pricing-v2")
    r1 = set_disposition(StageReview(stage="split", documents=(doc_a,)), Approved())
    r2 = set_disposition(StageReview(stage="split", documents=(doc_b,)), Rejected(reason="불필요"))

    approved = _collect_approved_documents([r1, r2])
    assert doc_a in approved
    assert doc_b not in approved


def test_T11_3_Rejected_개념은_link_입력_부재():
    """(9) Rejected 개념은 ④link 입력에 없어야 함"""
    doc_a = _make_split_doc("refund-policy")
    doc_b = _make_split_doc("pricing-v2")
    r1 = set_disposition(StageReview(stage="split", documents=(doc_a,)), Approved())
    r2 = set_disposition(StageReview(stage="split", documents=(doc_b,)), Rejected(reason="불필요"))

    approved = _collect_approved_documents([r1, r2])
    assert "pricing-v2" not in [d.concept_id for d in approved]


def test_T11_3_Rejected_산출은_admitted_입력_부재():
    """(10) Rejected된 review는 admit_okf 입력에 포함되지 않아야 함 — 승인분만 OkfDraft 구성"""
    doc_a = _make_split_doc("refund-policy")
    doc_b = _make_split_doc("pricing-v2")
    r1 = set_disposition(StageReview(stage="split", documents=(doc_a,)), Approved())
    r2 = set_disposition(StageReview(stage="split", documents=(doc_b,)), Rejected(reason="불필요"))

    approved = _collect_approved_documents([r1, r2])
    assert len(approved) == 1
    assert approved[0].concept_id == "refund-policy"


# ── T11.3 admission 닫힘 ──────────────────────────────────────────────────────


def test_T11_3_admission_모든_edge_존재_concept_violations_빈():
    """(11) 모든 edge가 존재하는 concept를 가리키면 violations 비어야 함 — publish 가능"""
    doc_a = _make_split_doc("refund-policy")
    doc_b = _make_split_doc("pricing-v2")
    edge = ConceptEdge(from_id="refund-policy", to_id="pricing-v2", relation="related")
    draft = OkfDraft(agent_id="cs-ops", documents=(doc_a, doc_b), edges=(edge,))
    card = _make_card(domains=["환불", "배송"])

    result = admit_okf(draft, card)
    assert result.violations == ()


def test_T11_3_admission_dangling_edge_violations():
    """(12) 잔여 dangling edge → violations 채워짐 — seam 미진입"""
    doc_a = _make_split_doc("refund-policy")
    # doc_b 없음 — pricing-v2를 가리키는 edge가 dangling
    edge = ConceptEdge(from_id="refund-policy", to_id="pricing-v2", relation="related")
    draft = OkfDraft(agent_id="cs-ops", documents=(doc_a,), edges=(edge,))
    card = _make_card(domains=["환불", "배송"])

    result = admit_okf(draft, card)
    assert len(result.violations) > 0


# ── T11.3 admission 권한 ──────────────────────────────────────────────────────


def test_T11_3_over_claim_domain_dropped_concepts_필터():
    """(13) over-claim domain concept → dropped_concepts 필터(전체 거부 아님)"""
    doc_owned = _make_split_doc("refund-policy", domain="환불")  # 권한 있음
    doc_over = _make_split_doc("tax-policy", domain="세무")  # 권한 없음
    draft = OkfDraft(agent_id="cs-ops", documents=(doc_owned, doc_over))
    card = _make_card(domains=["환불", "배송"])  # "세무" 없음

    result = admit_okf(draft, card)
    assert "tax-policy" in result.dropped_concepts
    assert "refund-policy" not in result.dropped_concepts
    # admitted에는 권한 있는 것만
    admitted_ids = {d.concept_id for d in result.admitted.documents}
    assert "refund-policy" in admitted_ids
    assert "tax-policy" not in admitted_ids


def test_T11_3_over_claim_탓_dangling_edge_dropped_edges():
    """(14) over-claim 제거로 dangling된 edge → dropped_edges"""
    doc_owned = _make_split_doc("refund-policy", domain="환불")
    doc_over = _make_split_doc("tax-policy", domain="세무")  # 권한 없음 → 제거됨
    edge = ConceptEdge(from_id="refund-policy", to_id="tax-policy", relation="related")
    draft = OkfDraft(agent_id="cs-ops", documents=(doc_owned, doc_over), edges=(edge,))
    card = _make_card(domains=["환불", "배송"])

    result = admit_okf(draft, card)
    assert edge in result.dropped_edges
    assert result.violations == ()  # 제거된 탓 dangling이라 violations 아님


def test_T11_3_under_claim_보존():
    """(15) under-claim(권한 안쪽 일부만 사용) — 권한 있는 것은 보존"""
    doc_a = _make_split_doc("refund-policy", domain="환불")
    # "배송" domain도 권한 있지만 사용 안 함 — under-claim은 OK
    draft = OkfDraft(agent_id="cs-ops", documents=(doc_a,))
    card = _make_card(domains=["환불", "배송"])

    result = admit_okf(draft, card)
    assert "refund-policy" not in result.dropped_concepts
    assert result.violations == ()


def test_T11_3_cannot_answer_domain_거부():
    """(16) cannot_answer에 있는 domain은 domain_authorized 정합 거부"""
    doc = _make_split_doc("refund-policy", domain="환불")
    draft = OkfDraft(agent_id="cs-ops", documents=(doc,))
    # 환불이 domains에는 있지만 cannot_answer에도 있음
    card = _make_card(domains=["환불", "배송"], cannot_answer=["환불"])

    result = admit_okf(draft, card)
    assert "refund-policy" in result.dropped_concepts


# ── T11.3 admission okf_index 정합 (round-trip·tmp_path) ─────────────────────


def test_T11_3_render_parse_같은_id_재도출(tmp_path: Path):
    """(17) render → 파싱 → 같은 id 재도출"""
    from datetime import datetime, timezone

    from agent_org_network.okf_index import build_knowledge_index_from_okf

    doc = _make_split_doc("refund-policy", domain="환불")
    card = _make_card(agent_id="cs-ops", domains=["환불"])

    content = render_okf_markdown(doc)
    agent_dir = tmp_path / "cs-ops"
    agent_dir.mkdir()
    (agent_dir / "refund-policy.md").write_text(content, encoding="utf-8")

    idx = build_knowledge_index_from_okf(
        card, tmp_path, generated_at=datetime.now(timezone.utc)
    )
    concept_ids = [c.id for c in idx.concepts]
    assert "refund-policy" in concept_ids


def test_T11_3_domain_tags에_실려_같은_domain_재도출(tmp_path: Path):
    """(18) domain이 tags에 실려 같은 domain 재도출(비대칭 정합)"""
    from datetime import datetime, timezone

    from agent_org_network.okf_index import build_knowledge_index_from_okf

    doc = _make_split_doc("refund-policy", domain="환불")
    card = _make_card(agent_id="cs-ops", domains=["환불"])

    content = render_okf_markdown(doc)
    agent_dir = tmp_path / "cs-ops"
    agent_dir.mkdir()
    (agent_dir / "refund-policy.md").write_text(content, encoding="utf-8")

    idx = build_knowledge_index_from_okf(
        card, tmp_path, generated_at=datetime.now(timezone.utc)
    )
    concept = next(c for c in idx.concepts if c.id == "refund-policy")
    assert concept.domain == "환불"


def test_T11_3_core_question_정합(tmp_path: Path):
    """(19) core_question round-trip — render 후 파싱한 core_question이 도출됨"""
    from datetime import datetime, timezone

    from agent_org_network.okf_index import build_knowledge_index_from_okf

    doc = OkfDocumentDraft(
        concept_id="refund-policy",
        title="환불 정책",
        body="7일 이내 환불 가능합니다.",
        core_question="환불이 가능한가요?",
        domain="환불",
    )
    card = _make_card(agent_id="cs-ops", domains=["환불"])

    content = render_okf_markdown(doc)
    agent_dir = tmp_path / "cs-ops"
    agent_dir.mkdir()
    (agent_dir / "refund-policy.md").write_text(content, encoding="utf-8")

    idx = build_knowledge_index_from_okf(
        card, tmp_path, generated_at=datetime.now(timezone.utc)
    )
    concept = next(c for c in idx.concepts if c.id == "refund-policy")
    # okf_index는 title + description으로 core_question 재도출
    # render 시 core_question → description, title → title
    # 따라서 build_core_question("환불 정책", "환불이 가능한가요?", stem) 결과가 나와야 함
    assert "환불 정책" in concept.core_question
    assert "환불이 가능한가요?" in concept.core_question


def test_T11_3_unsafe_stem_T11_1_이미_거부_회귀_가드():
    """(20) unsafe stem은 T11.1에서 이미 거부 — OkfDocumentDraft 구성 실패"""
    with pytest.raises(Exception):
        OkfDocumentDraft(
            concept_id="../escape",
            title="제목",
            body="내용",
            core_question="질문",
            domain="환불",
        )


# ── T11.3 실패 거동 ───────────────────────────────────────────────────────────


def test_T11_3_admission_실패는_AdmissionResult_반환_예외_아님():
    """(21) admission 실패는 AdmissionResult(violations) 반환 — 예외 아님"""
    doc = _make_split_doc("refund-policy", domain="세무")  # 권한 없음
    edge = ConceptEdge(from_id="refund-policy", to_id="nonexistent", relation="related")
    draft = OkfDraft(agent_id="cs-ops", documents=(doc,), edges=(edge,))
    card = _make_card(domains=["환불"])  # "세무" 없음

    # 예외가 아니라 AdmissionResult 반환
    result = admit_okf(draft, card)
    assert isinstance(result, AdmissionResult)


def test_T11_3_admission_성공은_violations_빈():
    """(22) 성공은 violations=() + 닫힌 권한통과 admitted"""
    doc_a = _make_split_doc("refund-policy", domain="환불")
    doc_b = _make_split_doc("shipping", domain="배송")
    edge = ConceptEdge(from_id="refund-policy", to_id="shipping", relation="related")
    draft = OkfDraft(agent_id="cs-ops", documents=(doc_a, doc_b), edges=(edge,))
    card = _make_card(domains=["환불", "배송"])

    result = admit_okf(draft, card)
    assert result.violations == ()
    assert result.dropped_concepts == ()
    assert result.dropped_edges == ()


# ── T11.3 commit seam ─────────────────────────────────────────────────────────


def test_T11_3_prepare_commit_request_doc_OkfFile_매핑_author_agent_id_보존():
    """(23) prepare_commit_request가 doc→OkfFile 매핑·author=owner·agent_id 보존"""
    from agent_org_network.git_gateway import BuilderCommitRequest, OkfFile

    doc_a = _make_split_doc("refund-policy", domain="환불")
    doc_b = _make_split_doc("pricing-v2", domain="배송")
    draft = OkfDraft(agent_id="cs-ops", documents=(doc_a, doc_b))
    card = _make_card(agent_id="cs-ops", domains=["환불", "배송"])

    result = admit_okf(draft, card)
    req = prepare_commit_request(result.admitted, owner="sungwoon")

    assert isinstance(req, BuilderCommitRequest)
    assert req.agent_id == "cs-ops"
    assert req.owner == "sungwoon"
    assert len(req.files) == 2
    paths = {f.path for f in req.files}
    assert "refund-policy.md" in paths
    assert "pricing-v2.md" in paths
    for f in req.files:
        assert isinstance(f, OkfFile)
        assert len(f.content) > 0


def test_T11_3_실_commit_okf_bundle_미호출_gateway_미주입_가드():
    """(24) prepare_commit_request는 실 commit_okf_bundle 호출 안 함 — gateway 인자 없음"""
    import inspect

    sig = inspect.signature(prepare_commit_request)
    params = list(sig.parameters.keys())
    assert "gateway" not in params
    assert "propagator" not in params


# ── T11.3 B1 fix: render_okf_markdown YAML 이스케이프 ────────────────────────


def test_T11_3_B1_core_question_콜론_포함_round_trip(tmp_path: Path):
    """B1: core_question에 콜론 포함 → render → parse → 원값 복원"""
    from datetime import datetime, timezone

    from agent_org_network.okf_index import build_knowledge_index_from_okf

    doc = OkfDocumentDraft(
        concept_id="refund-policy",
        title="환불 정책",
        body="상세 내용.",
        core_question="환불 조건: 7일 이내?",  # 콜론 포함
        domain="환불",
    )
    card = _make_card(agent_id="cs-ops", domains=["환불"])

    content = render_okf_markdown(doc)
    agent_dir = tmp_path / "cs-ops"
    agent_dir.mkdir()
    (agent_dir / "refund-policy.md").write_text(content, encoding="utf-8")

    idx = build_knowledge_index_from_okf(
        card, tmp_path, generated_at=datetime.now(timezone.utc)
    )
    assert len(idx.concepts) == 1, "콜론 때문에 프론트매터 파싱 실패 → concepts 없음"
    concept = idx.concepts[0]
    # description이 core_question 값을 그대로 담아야 함
    assert "환불 조건: 7일 이내?" in concept.core_question


def test_T11_3_B1_title_콜론_포함_round_trip(tmp_path: Path):
    """B1: title에 콜론 포함 → render → parse → title 복원"""
    from datetime import datetime, timezone

    from agent_org_network.okf_index import build_knowledge_index_from_okf

    doc = OkfDocumentDraft(
        concept_id="api-usage",
        title="API: 사용법",  # 콜론 포함
        body="사용 방법 안내.",
        core_question="API를 어떻게 사용하나요?",
        domain="환불",
    )
    card = _make_card(agent_id="cs-ops", domains=["환불"])

    content = render_okf_markdown(doc)
    agent_dir = tmp_path / "cs-ops"
    agent_dir.mkdir()
    (agent_dir / "api-usage.md").write_text(content, encoding="utf-8")

    idx = build_knowledge_index_from_okf(
        card, tmp_path, generated_at=datetime.now(timezone.utc)
    )
    assert len(idx.concepts) == 1, "title 콜론 때문에 프론트매터 파싱 실패"
    concept = idx.concepts[0]
    assert concept.label == "API: 사용법"


def test_T11_3_B1_domain_콜론_포함_tags_round_trip(tmp_path: Path):
    """B1: domain에 콜론 포함 → tags에 안전하게 실려 domain 재도출"""
    from datetime import datetime, timezone

    from agent_org_network.okf_index import build_knowledge_index_from_okf

    doc = OkfDocumentDraft(
        concept_id="pricing",
        title="가격 정책",
        body="표준 요금입니다.",
        core_question="가격은?",
        domain="가격: 표준",  # 콜론 포함 domain
    )
    card = _make_card(agent_id="cs-ops", domains=["가격: 표준"])

    content = render_okf_markdown(doc)
    agent_dir = tmp_path / "cs-ops"
    agent_dir.mkdir()
    (agent_dir / "pricing.md").write_text(content, encoding="utf-8")

    idx = build_knowledge_index_from_okf(
        card, tmp_path, generated_at=datetime.now(timezone.utc)
    )
    assert len(idx.concepts) == 1, "domain 콜론 때문에 tags 파싱 실패"
    concept = idx.concepts[0]
    assert concept.domain == "가격: 표준"


def test_T11_3_B1_샵_포함_round_trip(tmp_path: Path):
    """B1: title에 # 포함 → render → parse → title 복원"""
    from datetime import datetime, timezone

    from agent_org_network.okf_index import build_knowledge_index_from_okf

    doc = OkfDocumentDraft(
        concept_id="channel",
        title="채널 #general 안내",  # # 포함
        body="채널 사용법.",
        core_question="채널은 무엇인가?",
        domain="환불",
    )
    card = _make_card(agent_id="cs-ops", domains=["환불"])

    content = render_okf_markdown(doc)
    agent_dir = tmp_path / "cs-ops"
    agent_dir.mkdir()
    (agent_dir / "channel.md").write_text(content, encoding="utf-8")

    idx = build_knowledge_index_from_okf(
        card, tmp_path, generated_at=datetime.now(timezone.utc)
    )
    assert len(idx.concepts) == 1, "# 때문에 프론트매터 파싱 실패"
    concept = idx.concepts[0]
    assert "채널 #general 안내" in concept.core_question


# ── T11.3 M1 fix: set_disposition Edited payload 완전 검증 ───────────────────


def test_T11_3_M1_빈_Edited_거부():
    """M1: 빈 Edited()(documents=()·edges=()) → ValueError(어느 단계든)"""
    review = _make_stage_review_split()
    with pytest.raises(ValueError, match="Edited.*비어|payload.*불일치|documents.*비어"):
        set_disposition(review, Edited())


def test_T11_3_M1_split_단계_혼합_Edited_거부():
    """M1: split 단계에 Edited(documents+edges) 혼합 → ValueError"""
    doc = _make_split_doc("refund-policy")
    edge = ConceptEdge(from_id="refund-policy", to_id="pricing-v2", relation="related")
    review = StageReview(stage="split", documents=(doc,))
    with pytest.raises(ValueError, match="단계.*불일치|payload.*불일치|edges.*금지"):
        set_disposition(review, Edited(documents=(doc,), edges=(edge,)))


def test_T11_3_M1_link_단계_Edited_documents만_채우면_거부():
    """M1: link 단계에 Edited(documents=...) → ValueError(이미 있던 테스트 강화)"""
    doc = _make_split_doc("refund-policy")
    review = StageReview(
        stage="link",
        edges=(ConceptEdge(from_id="a", to_id="b", relation="related"),),
    )
    with pytest.raises(ValueError, match="단계.*불일치|payload.*불일치|link.*documents"):
        set_disposition(review, Edited(documents=(doc,)))


# ── T11.4 KnowledgeIndex 생성 합류 ───────────────────────────────────────────


from datetime import datetime, timezone  # noqa: E402


def _dt(offset_secs: float = 0.0) -> datetime:
    return datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc).replace(
        second=int(offset_secs)
    )


# A. round-trip 핵심 테스트


def test_T11_4_A1_admitted_1doc_인덱스_concept_1건_id_일치(tmp_path: Path):
    """A1: admitted 1 doc → 인덱스 concept 1건·id == concept_id"""
    from agent_org_network.okf_authoring import build_index_from_admitted

    doc = _make_split_doc("refund-policy", domain="환불")
    admitted = OkfDraft(agent_id="cs-ops", documents=(doc,))
    card = _make_card(agent_id="cs-ops", domains=["환불"])

    idx = build_index_from_admitted(admitted, card, tmp_path, generated_at=_dt())

    assert len(idx.concepts) == 1
    assert idx.concepts[0].id == "refund-policy"


def test_T11_4_A2_domain_tags_domain_재도출_정합(tmp_path: Path):
    """A2: domain → tags → domain 재도출 정합"""
    from agent_org_network.okf_authoring import build_index_from_admitted

    doc = _make_split_doc("shipping-policy", domain="배송")
    admitted = OkfDraft(agent_id="cs-ops", documents=(doc,))
    card = _make_card(agent_id="cs-ops", domains=["배송"])

    idx = build_index_from_admitted(admitted, card, tmp_path, generated_at=_dt())

    assert idx.concepts[0].domain == "배송"


def test_T11_4_A3_core_question_정합_title_description(tmp_path: Path):
    """A3: core_question 정합 — title+description round-trip"""
    from agent_org_network.okf_authoring import build_index_from_admitted

    doc = OkfDocumentDraft(
        concept_id="refund-policy",
        title="환불 정책",
        body="7일 이내 환불 가능.",
        core_question="환불이 가능한가요?",
        domain="환불",
    )
    admitted = OkfDraft(agent_id="cs-ops", documents=(doc,))
    card = _make_card(agent_id="cs-ops", domains=["환불"])

    idx = build_index_from_admitted(admitted, card, tmp_path, generated_at=_dt())

    concept = idx.concepts[0]
    assert "환불 정책" in concept.core_question
    assert "환불이 가능한가요?" in concept.core_question


def test_T11_4_A4_다중_doc_파일명_정렬_순서(tmp_path: Path):
    """A4: 다중 doc — 파일명 정렬 순서로 concept 배열"""
    from agent_org_network.okf_authoring import build_index_from_admitted

    doc_b = _make_split_doc("b-shipping", domain="배송")
    doc_a = _make_split_doc("a-refund", domain="환불")
    admitted = OkfDraft(agent_id="cs-ops", documents=(doc_b, doc_a))
    card = _make_card(agent_id="cs-ops", domains=["환불", "배송"])

    idx = build_index_from_admitted(admitted, card, tmp_path, generated_at=_dt())

    assert len(idx.concepts) == 2
    # 파일명 정렬: a-refund < b-shipping
    assert idx.concepts[0].id == "a-refund"
    assert idx.concepts[1].id == "b-shipping"


def test_T11_4_A5_같은_입력_같은_인덱스_멱등(tmp_path: Path):
    """A5: 같은 admitted + generated_at → 같은 인덱스(멱등)"""
    from agent_org_network.okf_authoring import build_index_from_admitted

    doc = _make_split_doc("refund-policy", domain="환불")
    admitted = OkfDraft(agent_id="cs-ops", documents=(doc,))
    card = _make_card(agent_id="cs-ops", domains=["환불"])
    ts = _dt()

    idx1 = build_index_from_admitted(admitted, card, tmp_path / "run1", generated_at=ts)
    idx2 = build_index_from_admitted(admitted, card, tmp_path / "run2", generated_at=ts)

    assert idx1.concepts == idx2.concepts
    assert idx1.agent_id == idx2.agent_id


# B. admitted 필터 정합


def test_T11_4_B6_over_claim_섞인_draft_인덱스에_over_claim_부재(tmp_path: Path):
    """B6: over-claim 섞인 draft → admit_okf → 합류 인덱스에 over-claim concept 부재"""
    from agent_org_network.okf_authoring import build_index_from_admitted

    doc_ok = _make_split_doc("refund-policy", domain="환불")
    doc_over = _make_split_doc("tax-policy", domain="세무")  # over-claim
    draft = OkfDraft(agent_id="cs-ops", documents=(doc_ok, doc_over))
    card = _make_card(agent_id="cs-ops", domains=["환불"])  # "세무" 없음

    result = admit_okf(draft, card)
    assert "tax-policy" in result.dropped_concepts

    idx = build_index_from_admitted(result.admitted, card, tmp_path, generated_at=_dt())

    concept_ids = [c.id for c in idx.concepts]
    assert "tax-policy" not in concept_ids
    assert "refund-policy" in concept_ids


# C. PublishIndex 테스트


def test_T11_4_C7_to_publish_index_PublishIndex_정상_agent_id_보존(tmp_path: Path):
    """C7: to_publish_index → PublishIndex 정상·agent_id 보존"""
    from agent_org_network.okf_authoring import build_index_from_admitted, to_publish_index
    from agent_org_network.transport import PublishIndex

    doc = _make_split_doc("refund-policy", domain="환불")
    admitted = OkfDraft(agent_id="cs-ops", documents=(doc,))
    card = _make_card(agent_id="cs-ops", domains=["환불"])

    idx = build_index_from_admitted(admitted, card, tmp_path, generated_at=_dt())
    frame = to_publish_index(idx)

    assert isinstance(frame, PublishIndex)
    assert frame.index.agent_id == "cs-ops"


def test_T11_4_C8_accept_published_index_올바른_owner_store_보관_타_owner_거부(
    tmp_path: Path,
):
    """C8: PublishIndex → accept_published_index — 올바른 owner면 store 보관·타 owner 거부"""
    from agent_org_network.okf_authoring import build_index_from_admitted, to_publish_index
    from agent_org_network.registry import Registry
    from agent_org_network.two_stage_router import (
        InMemoryPublishedIndexStore,
        accept_published_index,
    )

    doc = _make_split_doc("refund-policy", domain="환불")
    admitted = OkfDraft(agent_id="cs-ops", documents=(doc,))
    card = _make_card(agent_id="cs-ops", domains=["환불"])

    registry = Registry()
    registry.register(card)
    store = InMemoryPublishedIndexStore()

    idx = build_index_from_admitted(admitted, card, tmp_path, generated_at=_dt())
    frame = to_publish_index(idx)

    # 올바른 owner(sungwoon) → 수용
    ok = accept_published_index("sungwoon", frame.index, registry, store)
    assert ok is True
    assert store.get("cs-ops") is not None

    # 타 owner → 거부
    store2 = InMemoryPublishedIndexStore()
    rejected = accept_published_index("other-owner", frame.index, registry, store2)
    assert rejected is False
    assert store2.get("cs-ops") is None


# D. 빈 admitted 비대칭(m2)


def test_T11_4_D9_전부_over_claim_빈_인덱스_PublishIndex_구성_OK(tmp_path: Path):
    """D9: 전부 over-claim → admitted.documents=() → 빈 인덱스(concepts=())·PublishIndex 구성 OK"""
    from agent_org_network.okf_authoring import build_index_from_admitted, to_publish_index
    from agent_org_network.transport import PublishIndex

    doc = _make_split_doc("tax-policy", domain="세무")  # 전부 over-claim
    draft = OkfDraft(agent_id="cs-ops", documents=(doc,))
    card = _make_card(agent_id="cs-ops", domains=["환불"])

    result = admit_okf(draft, card)
    assert result.admitted.documents == ()

    idx = build_index_from_admitted(result.admitted, card, tmp_path, generated_at=_dt())
    assert idx.concepts == ()

    frame = to_publish_index(idx)
    assert isinstance(frame, PublishIndex)
    assert frame.index.agent_id == "cs-ops"


def test_T11_4_D10_빈_admitted_commit_경로_거부(tmp_path: Path):
    """D10: 빈 admitted → prepare_commit_request → commit_okf_bundle(FakeGitGateway) → ValueError"""
    from agent_org_network.git_gateway import FakeGitGateway, commit_okf_bundle

    admitted_empty = OkfDraft(agent_id="cs-ops", documents=())
    req = prepare_commit_request(admitted_empty, owner="sungwoon")

    gateway = FakeGitGateway()
    with pytest.raises(ValueError, match="파일.*없|files.*비"):
        commit_okf_bundle(req, gateway)


# E. violations 가드


def test_T11_4_E11_violations_채워진_admission_publish_안_함(tmp_path: Path):
    """E11: violations 채워진 admission → publish_index_from_admission → None(인덱스·커밋 둘 다 막힘)"""
    from agent_org_network.okf_authoring import publish_index_from_admission

    # dangling edge → violations 발생
    doc = _make_split_doc("refund-policy", domain="환불")
    edge = ConceptEdge(from_id="refund-policy", to_id="nonexistent", relation="related")
    draft = OkfDraft(agent_id="cs-ops", documents=(doc,), edges=(edge,))
    card = _make_card(agent_id="cs-ops", domains=["환불"])

    result = admit_okf(draft, card)
    assert result.violations  # violations 있음

    frame = publish_index_from_admission(result, card, tmp_path, generated_at=_dt())
    assert frame is None


# M1·M2 (code-review T11.4 보강)


def test_T11_4_M1_version_round_trip(tmp_path: Path):
    """M1: 비기본 version이 PublishIndex.index.version까지 보존"""
    from agent_org_network.okf_authoring import build_index_from_admitted, to_publish_index

    doc = _make_split_doc("refund-policy", domain="환불")
    admitted = OkfDraft(agent_id="cs-ops", documents=(doc,))
    card = _make_card(agent_id="cs-ops", domains=["환불"])

    idx = build_index_from_admitted(
        admitted, card, tmp_path, generated_at=_dt(), version="okf-2"
    )
    frame = to_publish_index(idx)
    assert frame.index.version == "okf-2"


def test_T11_4_M2_to_publish_index_agent_id_보존_단위():
    """M2: to_publish_index 단독 — 임의 KnowledgeIndex의 agent_id 보존(통째 래핑)"""
    from agent_org_network.knowledge_index import KnowledgeIndex
    from agent_org_network.okf_authoring import to_publish_index
    from agent_org_network.transport import PublishIndex

    idx = KnowledgeIndex(
        agent_id="cs-ops",
        version="okf-1",
        generated_at=_dt(),
        concepts=(),
    )
    frame = to_publish_index(idx)
    assert isinstance(frame, PublishIndex)
    assert frame.index is idx
    assert frame.index.agent_id == "cs-ops"
