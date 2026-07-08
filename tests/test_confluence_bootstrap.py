"""S1 — Confluence 오너십 부트스트랩 도출(`derive_card_candidates`) red→green 테스트.

ADR 0039 결정 4의 절대 불변식(모듈 docstring 4종)을 테스트로 잠근다:
  1. 후보 생성일 뿐 권한 생성이 아니다.
  2. over-claim은 중앙 수용(`domain_authorized`)이 필터 — 이 모듈은 좁히지 않는다.
  3. `AgentCardCandidate` != `AgentCard`(team·last_reviewed_at 결여로 직접 등록 불가).
  4. `proposed_owner`는 제안일 뿐 User 실재 검증은 이 모듈이 하지 않는다.
"""

from datetime import date

import pytest
from pydantic import ValidationError

from agent_org_network.agent_card import AgentCard, domain_authorized
from agent_org_network.confluence_bootstrap import (
    ConfluenceOwnershipSignal,
    ConfluenceProvenance,
    derive_card_candidates,
)


def test_single_admin_single_candidate() -> None:
    signal = ConfluenceOwnershipSignal(
        space_key="CS Ops",
        space_name="CS Ops 팀 스페이스",
        space_admins=("alice",),
        labels=("refund", "billing"),
    )
    label_domain_map = {"billing": "billing", "refund": "refund"}

    result = derive_card_candidates(signal, label_domain_map)

    assert result.ambiguities == ()
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.agent_id == "CS-Ops"
    assert candidate.proposed_owner == "alice"
    assert candidate.candidate_domains == ("billing", "refund")
    assert candidate.provenance == ConfluenceProvenance(
        space_key="CS Ops",
        owner_signal="space_admin",
        source_labels=("refund", "billing"),
    )


def test_multiple_admins_emits_ambiguity() -> None:
    signal = ConfluenceOwnershipSignal(
        space_key="eng",
        space_name="Engineering",
        space_admins=("alice", "bob"),
    )

    result = derive_card_candidates(signal, {})

    assert result.candidates == ()
    kinds = [a.kind for a in result.ambiguities]
    assert kinds == ["multiple_space_admins"]


def test_no_owner_signal() -> None:
    signal = ConfluenceOwnershipSignal(space_key="eng", space_name="Engineering")

    result = derive_card_candidates(signal, {})

    assert result.candidates == ()
    assert [a.kind for a in result.ambiguities] == ["no_owner_signal"]


def test_unmapped_label_excluded_and_reported() -> None:
    signal = ConfluenceOwnershipSignal(
        space_key="eng",
        space_name="Engineering",
        space_admins=("alice",),
        labels=("billing", "mystery"),
    )

    result = derive_card_candidates(signal, {"billing": "billing"})

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.candidate_domains == ("billing",)

    unmapped = [a for a in result.ambiguities if a.kind == "unmapped_label"]
    assert len(unmapped) == 1
    assert "mystery" in unmapped[0].detail


def test_space_key_underivable() -> None:
    signal = ConfluenceOwnershipSignal(
        space_key="!!!",
        space_name="Weird Space",
        space_admins=("alice",),
    )

    result = derive_card_candidates(signal, {})

    assert result.candidates == ()
    kinds = [a.kind for a in result.ambiguities]
    assert kinds == ["agent_id_underivable"]


def test_candidate_is_not_registrable_directly() -> None:
    signal = ConfluenceOwnershipSignal(
        space_key="eng",
        space_name="Engineering",
        space_admins=("alice",),
        labels=("billing",),
    )

    result = derive_card_candidates(signal, {"billing": "billing"})
    candidate = result.candidates[0]
    dumped = candidate.model_dump()

    assert "team" not in dumped
    assert "last_reviewed_at" not in dumped
    with pytest.raises(ValidationError):
        AgentCard(**dumped)


def test_broad_domains_pass_through_then_central_filter() -> None:
    signal = ConfluenceOwnershipSignal(
        space_key="eng",
        space_name="Engineering",
        space_admins=("alice",),
        labels=("billing", "security", "hr"),
    )
    label_domain_map = {"billing": "billing", "security": "security", "hr": "hr"}

    result = derive_card_candidates(signal, label_domain_map)
    candidate = result.candidates[0]
    assert candidate.candidate_domains == ("billing", "hr", "security")

    card = AgentCard(
        agent_id=candidate.agent_id,
        owner=candidate.proposed_owner,
        team="Support",
        summary=candidate.summary,
        domains=["billing"],
        last_reviewed_at=date(2026, 7, 8),
    )

    assert domain_authorized("billing", card) is True
    assert domain_authorized("security", card) is False
    assert domain_authorized("hr", card) is False


def test_pure_deterministic() -> None:
    signal = ConfluenceOwnershipSignal(
        space_key="eng",
        space_name="Engineering",
        space_admins=("alice",),
        labels=("billing", "mystery"),
    )
    label_domain_map = {"billing": "billing"}

    result1 = derive_card_candidates(signal, label_domain_map)
    result2 = derive_card_candidates(signal, label_domain_map)

    assert result1 == result2


def test_duplicate_space_admins_collapsed_to_single() -> None:
    signal = ConfluenceOwnershipSignal(
        space_key="eng",
        space_name="Engineering",
        space_admins=("alice", "alice"),
    )

    result = derive_card_candidates(signal, {})

    assert result.ambiguities == ()
    assert len(result.candidates) == 1
    assert result.candidates[0].proposed_owner == "alice"


def test_empty_labels_no_domains_no_ambiguity() -> None:
    signal = ConfluenceOwnershipSignal(
        space_key="eng",
        space_name="Engineering",
        space_admins=("alice",),
    )

    result = derive_card_candidates(signal, {})

    assert result.ambiguities == ()
    assert result.candidates[0].candidate_domains == ()


def test_page_author_fallback_when_no_admins() -> None:
    signal = ConfluenceOwnershipSignal(
        space_key="eng",
        space_name="Engineering",
        page_authors=("carol",),
    )

    result = derive_card_candidates(signal, {})

    assert result.ambiguities == ()
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.proposed_owner == "carol"
    assert candidate.provenance.owner_signal == "page_author"


def test_multiple_page_authors_without_admins_emits_ambiguity() -> None:
    """관리자 0 · page_author 다수 — 임의 선택 금지(불변식). `multiple_space_admins`와
    대칭으로 전용 kind `multiple_page_authors`를 방출한다(M1 — domain-architect 결정 확정).
    """
    signal = ConfluenceOwnershipSignal(
        space_key="eng",
        space_name="Engineering",
        page_authors=("carol", "dave"),
    )

    result = derive_card_candidates(signal, {})

    assert result.candidates == ()
    assert [a.kind for a in result.ambiguities] == ["multiple_page_authors"]


def test_agent_id_normalization_strips_invalid_leading_chars() -> None:
    signal = ConfluenceOwnershipSignal(
        space_key="_cs.ops team",
        space_name="CS Ops",
        space_admins=("alice",),
    )

    result = derive_card_candidates(signal, {})

    assert result.candidates[0].agent_id == "cs-ops-team"


def test_ambiguities_are_sorted_stable_across_kinds() -> None:
    signal = ConfluenceOwnershipSignal(
        space_key="!!!",
        space_name="Weird Space",
        space_admins=("alice", "bob"),
        labels=("mystery",),
    )

    result1 = derive_card_candidates(signal, {})
    result2 = derive_card_candidates(signal, {})

    assert result1.candidates == ()
    kinds = [a.kind for a in result1.ambiguities]
    assert set(kinds) == {"multiple_space_admins", "agent_id_underivable", "unmapped_label"}
    assert result1 == result2


def test_overlong_space_key_truncated_then_revalidated() -> None:
    """M2 — 64자 초과 space_key는 절단 후 재검증되어 유효 agent_id로 정착한다(회귀 방어)."""
    space_key = "a" * 70

    signal = ConfluenceOwnershipSignal(
        space_key=space_key,
        space_name="Very Long Space",
        space_admins=("alice",),
    )

    result = derive_card_candidates(signal, {})

    assert result.ambiguities == ()
    assert len(result.candidates) == 1
    assert result.candidates[0].agent_id == "a" * 64


def test_blank_proposed_owner_treated_as_no_owner_signal() -> None:
    """m2 — 공백뿐인 space_admins 항목은 owner 신호로 세지 않는다(빈 후보 방출 금지)."""
    signal = ConfluenceOwnershipSignal(
        space_key="eng",
        space_name="Engineering",
        space_admins=("   ",),
    )

    result = derive_card_candidates(signal, {})

    assert result.candidates == ()
    assert [a.kind for a in result.ambiguities] == ["no_owner_signal"]


def test_empty_string_domain_filtered_from_candidate_domains() -> None:
    """m2 — label_domain_map이 빈 문자열 domain을 주면 candidate_domains에서 제외한다."""
    signal = ConfluenceOwnershipSignal(
        space_key="eng",
        space_name="Engineering",
        space_admins=("alice",),
        labels=("billing", "noise"),
    )

    result = derive_card_candidates(signal, {"billing": "billing", "noise": ""})

    assert result.ambiguities == ()
    assert result.candidates[0].candidate_domains == ("billing",)
