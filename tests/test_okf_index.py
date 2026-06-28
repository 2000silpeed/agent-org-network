"""OKF→KnowledgeIndex 어댑터(okf_index.build_knowledge_index_from_okf) 결정론 테스트.

게이트 내·공급자 SDK 0(stdlib+pyyaml). tmp OKF 번들을 만들어 도출 규칙을 단언한다:
  - core_question에 title·description 토큰 포함.
  - domain ∈ card.domains(tags 교집합 첫 태그 or domains[0] 폴백).
  - 파일명 정렬(concept 순서 결정론).
  - 빈 OKF(디렉터리/문서 없음) → 빈 인덱스.
  - card.domains 비면 그 문서 skip(권한 불가).
  - 같은 OKF·같은 generated_at → 같은 인덱스(결정론).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from agent_org_network.agent_card import AgentCard
from agent_org_network.okf_index import build_knowledge_index_from_okf

_NOW = datetime(2026, 6, 28, 0, 0, 0, tzinfo=timezone.utc)
_REVIEWED = date(2026, 6, 28)


def _card(agent_id: str, domains: list[str]) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner="owner1",
        team="ops",
        summary=f"{agent_id} 요약",
        domains=domains,
        last_reviewed_at=_REVIEWED,
    )


def _write_doc(
    okf_root: Path,
    agent_id: str,
    filename: str,
    *,
    front: str,
    body: str = "본문",
) -> None:
    agent_dir = okf_root / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / filename).write_text(f"---\n{front}\n---\n\n{body}\n", encoding="utf-8")


# ── core_question·domain 도출 ────────────────────────────────────────────────


def test_concept_core_question_includes_title_and_description(tmp_path: Path) -> None:
    """core_question = "title. description" — 매칭 토큰 확보."""
    card = _card("cs_ops", ["환불", "보상"])
    _write_doc(
        tmp_path,
        "cs_ops",
        "refund-policy.md",
        front="type: policy\ntitle: 환불 정책\ndescription: 결제 후 환불 가능 기간과 수수료\ntags: [환불, 보상, cs]",
    )

    index = build_knowledge_index_from_okf(card, tmp_path, generated_at=_NOW)

    assert len(index.concepts) == 1
    concept = index.concepts[0]
    assert concept.id == "refund-policy"
    assert concept.label == "환불 정책"
    assert "환불" in concept.core_question
    assert "정책" in concept.core_question
    assert "수수료" in concept.core_question  # description 토큰
    assert concept.type == "policy"


def test_domain_picks_first_tag_in_card_domains(tmp_path: Path) -> None:
    """domain = tags 중 card.domains에 든 첫 태그(선언 순서 우선)."""
    card = _card("cs_ops", ["환불", "보상"])
    # tags 순서: [보상, 환불, cs] → card.domains에 든 첫 태그 = 보상
    _write_doc(
        tmp_path,
        "cs_ops",
        "compensation.md",
        front="type: policy\ntitle: 보상 기준\ndescription: 장애 보상\ntags: [보상, 환불, cs]",
    )

    index = build_knowledge_index_from_okf(card, tmp_path, generated_at=_NOW)

    assert index.concepts[0].domain == "보상"
    assert index.concepts[0].domain in card.domains


def test_domain_falls_back_to_first_card_domain_when_no_tag_overlap(tmp_path: Path) -> None:
    """tags 교집합 없으면 domain = card.domains[0] 폴백(권한 도메인 보존)."""
    card = _card("contract_ops", ["계약 검토"])
    # tags=[계약, legal] — "계약" ≠ "계약 검토" → 교집합 없음 → 폴백 "계약 검토"
    _write_doc(
        tmp_path,
        "contract_ops",
        "standard-terms.md",
        front="type: policy\ntitle: 표준 계약 조건\ndescription: 계약 기간과 해지\ntags: [계약, legal]",
    )

    index = build_knowledge_index_from_okf(card, tmp_path, generated_at=_NOW)

    assert index.concepts[0].domain == "계약 검토"


def test_concepts_sorted_by_filename(tmp_path: Path) -> None:
    """concept 순서 = 파일명 정렬(결정론)."""
    card = _card("cs_ops", ["환불", "보상"])
    _write_doc(tmp_path, "cs_ops", "refund-policy.md", front="title: 환불\ndescription: d\ntags: [환불]")
    _write_doc(tmp_path, "cs_ops", "compensation.md", front="title: 보상\ndescription: d\ntags: [보상]")
    _write_doc(tmp_path, "cs_ops", "index.md", front="title: 목차\ndescription: d\ntags: [환불]")

    index = build_knowledge_index_from_okf(card, tmp_path, generated_at=_NOW)

    ids = [c.id for c in index.concepts]
    assert ids == sorted(ids)
    assert ids == ["compensation", "index", "refund-policy"]


# ── 폴백·빈 경로 ─────────────────────────────────────────────────────────────


def test_missing_okf_directory_yields_empty_index(tmp_path: Path) -> None:
    """OKF 디렉터리 없음 → concepts=() 빈 인덱스(미아 없음으로 자연 처리)."""
    card = _card("cs_ops", ["환불"])

    index = build_knowledge_index_from_okf(card, tmp_path, generated_at=_NOW)

    assert index.agent_id == "cs_ops"
    assert index.concepts == ()


def test_empty_okf_directory_yields_empty_index(tmp_path: Path) -> None:
    """OKF 디렉터리는 있으나 .md 문서 없음 → 빈 인덱스."""
    card = _card("cs_ops", ["환불"])
    (tmp_path / "cs_ops").mkdir(parents=True)

    index = build_knowledge_index_from_okf(card, tmp_path, generated_at=_NOW)

    assert index.concepts == ()


def test_card_with_no_domains_skips_document(tmp_path: Path) -> None:
    """card.domains 비면 domain을 정할 수 없어 그 문서 skip(권한 불가)."""
    card = _card("empty_ops", [])
    _write_doc(tmp_path, "empty_ops", "doc.md", front="title: 문서\ndescription: d\ntags: [무엇이든]")

    index = build_knowledge_index_from_okf(card, tmp_path, generated_at=_NOW)

    assert index.concepts == ()


def test_no_title_uses_stem_as_label_and_core_question(tmp_path: Path) -> None:
    """title·description 없으면 label·core_question은 stem 폴백(빈 값 금지)."""
    card = _card("cs_ops", ["환불"])
    _write_doc(tmp_path, "cs_ops", "bare.md", front="tags: [환불]")

    index = build_knowledge_index_from_okf(card, tmp_path, generated_at=_NOW)

    concept = index.concepts[0]
    assert concept.label == "bare"
    assert concept.core_question == "bare"
    assert concept.type is None


def test_no_frontmatter_uses_stem_and_first_domain(tmp_path: Path) -> None:
    """프론트매터 없는 문서 → stem 폴백 + domains[0](tags 없음 → 폴백)."""
    card = _card("cs_ops", ["환불"])
    (tmp_path / "cs_ops").mkdir(parents=True)
    (tmp_path / "cs_ops" / "plain.md").write_text("# 그냥 본문\n내용\n", encoding="utf-8")

    index = build_knowledge_index_from_okf(card, tmp_path, generated_at=_NOW)

    concept = index.concepts[0]
    assert concept.id == "plain"
    assert concept.label == "plain"
    assert concept.domain == "환불"


# ── 결정론 ───────────────────────────────────────────────────────────────────


def test_deterministic_same_okf_same_index(tmp_path: Path) -> None:
    """같은 OKF·같은 generated_at → 같은 인덱스(순수 함수)."""
    card = _card("cs_ops", ["환불", "보상"])
    _write_doc(
        tmp_path,
        "cs_ops",
        "refund-policy.md",
        front="type: policy\ntitle: 환불 정책\ndescription: d\ntags: [환불]",
    )

    a = build_knowledge_index_from_okf(card, tmp_path, generated_at=_NOW)
    b = build_knowledge_index_from_okf(card, tmp_path, generated_at=_NOW)

    assert a == b


def test_generated_at_and_version_injected(tmp_path: Path) -> None:
    """generated_at·version 주입값이 인덱스에 그대로 실린다."""
    card = _card("cs_ops", ["환불"])
    _write_doc(tmp_path, "cs_ops", "doc.md", front="title: t\ndescription: d\ntags: [환불]")

    index = build_knowledge_index_from_okf(
        card, tmp_path, generated_at=_NOW, version="okf-v2"
    )

    assert index.generated_at == _NOW
    assert index.version == "okf-v2"
