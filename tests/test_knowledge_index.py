"""KnowledgeIndex·Concept 값 객체 — T10.1 red→green 테스트.

슬라이스:
  (a) Concept frozen 값 객체 — id·label·core_question·type 검증
  (b) KnowledgeIndex frozen 값 객체 + admission 검증
      - agent_id: AgentCard와 동일 wire-format 규칙(공유 헬퍼 재사용)
      - version: 빈 문자열 거부
      - concepts: 중복 concept.id 거부·빈 튜플 허용
      - ConceptEdge: 최소 frozen 값 객체

결정론: 실 값 객체가 결정론 — FakeClassifier·StubRuntime 주입 불요.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agent_org_network.agent_card import AGENT_ID_MAX_LENGTH, AgentCard
from agent_org_network.knowledge_index import Concept, ConceptEdge, KnowledgeIndex

# ── 헬퍼 ────────────────────────────────────────────────────────────────────

_NOW = datetime(2026, 6, 27, 0, 0, 0, tzinfo=timezone.utc)


def _make_concept(
    concept_id: str = "c1",
    label: str = "입력 폼",
    core_question: str = "어떤 컴포넌트가 사용자 입력값을 캡처하나?",
    type_: str | None = None,
) -> Concept:
    return Concept(id=concept_id, label=label, core_question=core_question, type=type_)


def _make_index(
    agent_id: str = "cs_ops",
    version: str = "v1",
    concepts: tuple[Concept, ...] = (),
    edges: tuple[ConceptEdge, ...] = (),
) -> KnowledgeIndex:
    return KnowledgeIndex(
        agent_id=agent_id,
        version=version,
        generated_at=_NOW,
        concepts=concepts,
        edges=edges,
    )


# ════════════════════════════════════════════════════════════════════════════
# 1. Concept 값 객체
# ════════════════════════════════════════════════════════════════════════════


class TestConcept:
    """Concept frozen pydantic 값 객체 기본 동작."""

    def test_유효_Concept_생성_필드_보존(self) -> None:
        c = _make_concept(
            concept_id="c1",
            label="입력 폼",
            core_question="어떤 컴포넌트가 사용자 입력값을 캡처하나?",
            type_="form",
        )
        assert c.id == "c1"
        assert c.label == "입력 폼"
        assert c.core_question == "어떤 컴포넌트가 사용자 입력값을 캡처하나?"
        assert c.type == "form"

    def test_type_기본값_None(self) -> None:
        c = _make_concept(type_=None)
        assert c.type is None

    def test_frozen_수정_불가(self) -> None:
        c = _make_concept()
        with pytest.raises(Exception):
            c.id = "new_id"  # type: ignore[misc]

    # ── admission: 빈 / 공백 거부 ────────────────────────────────────────────

    def test_빈_id_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_concept(concept_id="")

    def test_공백만_id_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_concept(concept_id="   ")

    def test_빈_core_question_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_concept(core_question="")

    def test_공백만_core_question_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_concept(core_question="   ")


# ════════════════════════════════════════════════════════════════════════════
# 2. ConceptEdge 값 객체
# ════════════════════════════════════════════════════════════════════════════


class TestConceptEdge:
    """ConceptEdge 최소 frozen 값 객체."""

    def test_유효_ConceptEdge_생성_필드_보존(self) -> None:
        edge = ConceptEdge(from_id="c1", to_id="c2", relation="is_part_of")
        assert edge.from_id == "c1"
        assert edge.to_id == "c2"
        assert edge.relation == "is_part_of"

    def test_frozen_수정_불가(self) -> None:
        edge = ConceptEdge(from_id="c1", to_id="c2", relation="is_part_of")
        with pytest.raises(Exception):
            edge.from_id = "c3"  # type: ignore[misc]


# ════════════════════════════════════════════════════════════════════════════
# 3. KnowledgeIndex 값 객체 — 유효 생성
# ════════════════════════════════════════════════════════════════════════════


class TestKnowledgeIndexValid:
    """유효 KnowledgeIndex 생성·frozen·필드 보존."""

    def test_유효_인덱스_생성_필드_보존(self) -> None:
        c = _make_concept()
        idx = _make_index(agent_id="cs_ops", version="v1", concepts=(c,))
        assert idx.agent_id == "cs_ops"
        assert idx.version == "v1"
        assert idx.generated_at == _NOW
        assert len(idx.concepts) == 1
        assert idx.concepts[0] == c

    def test_edges_기본값_빈_튜플(self) -> None:
        idx = _make_index()
        assert idx.edges == ()

    def test_edges_주어지면_보존(self) -> None:
        edge = ConceptEdge(from_id="c1", to_id="c2", relation="narrows")
        idx = _make_index(edges=(edge,))
        assert len(idx.edges) == 1
        assert idx.edges[0] == edge

    def test_frozen_수정_불가(self) -> None:
        idx = _make_index()
        with pytest.raises(Exception):
            idx.agent_id = "other"  # type: ignore[misc]

    def test_빈_concepts_허용(self) -> None:
        """개념 없는 인덱스(에이전트가 아직 개념 없음)는 허용 — 0 후보로 자연 처리."""
        idx = _make_index(concepts=())
        assert idx.concepts == ()

    def test_다수_concepts_허용(self) -> None:
        c1 = _make_concept(concept_id="c1", core_question="질문 1")
        c2 = _make_concept(concept_id="c2", core_question="질문 2")
        idx = _make_index(concepts=(c1, c2))
        assert len(idx.concepts) == 2


# ════════════════════════════════════════════════════════════════════════════
# 4. KnowledgeIndex admission — 거부 단언
# ════════════════════════════════════════════════════════════════════════════


class TestKnowledgeIndexAdmission:
    """유효하지 않은 인덱스는 생성 거부(ValidationError)."""

    # ── agent_id admission ──────────────────────────────────────────────────

    def test_빈_agent_id_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_index(agent_id="")

    def test_공백_agent_id_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_index(agent_id="   ")

    def test_경로탈출_슬래시_agent_id_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_index(agent_id="a/b")

    def test_경로탈출_점점_agent_id_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_index(agent_id="..")

    def test_선행_하이픈_agent_id_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_index(agent_id="-cs")

    def test_선행_언더스코어_agent_id_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_index(agent_id="_cs")

    def test_후행_개행_agent_id_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_index(agent_id="cs_ops\n")

    def test_비ASCII_agent_id_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_index(agent_id="카드")

    def test_길이초과_agent_id_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_index(agent_id="a" * (AGENT_ID_MAX_LENGTH + 1))

    # ── version ──────────────────────────────────────────────────────────────

    def test_빈_version_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_index(version="")

    # ── concepts 중복 id ─────────────────────────────────────────────────────

    def test_중복_concept_id_거부(self) -> None:
        c1 = _make_concept(concept_id="same", core_question="질문 1")
        c2 = _make_concept(concept_id="same", core_question="질문 2")
        with pytest.raises(ValidationError):
            _make_index(concepts=(c1, c2))


# ════════════════════════════════════════════════════════════════════════════
# 5. agent_id 검증 — AgentCard와 동일 규칙 단언
# ════════════════════════════════════════════════════════════════════════════


class TestAgentIdRuleParity:
    """KnowledgeIndex.agent_id 검증이 AgentCard.agent_id와 동일 규칙임을 단언.

    같은 유효 입력 → 둘 다 수용.
    같은 무효 입력 → 둘 다 ValidationError.
    """

    _VALID_IDS = [
        "cs_ops",
        "contract_ops",
        "agent_X",
        "n",
        "A",
        "Z9_x-1",
        "a" * AGENT_ID_MAX_LENGTH,
    ]

    _INVALID_IDS = [
        "",
        "   ",
        "../x",
        "/abs",
        "a/b",
        "a\\b",
        "..",
        ".",
        "-x",
        "_x",
        "a b",
        "cs_ops\n",
        "카드",
        "café",
        "a" * (AGENT_ID_MAX_LENGTH + 1),
    ]

    def _make_agent_card(self, agent_id: str) -> AgentCard:
        from datetime import date

        return AgentCard(
            agent_id=agent_id,
            owner="alice",
            team="ops",
            summary="패리티 테스트",
            domains=["운영"],
            last_reviewed_at=date(2026, 6, 27),
        )

    @pytest.mark.parametrize("agent_id", _VALID_IDS)
    def test_유효_agent_id_둘_다_수용(self, agent_id: str) -> None:
        card = self._make_agent_card(agent_id)
        idx = _make_index(agent_id=agent_id)
        assert card.agent_id == idx.agent_id

    @pytest.mark.parametrize("agent_id", _INVALID_IDS)
    def test_무효_agent_id_둘_다_ValidationError(self, agent_id: str) -> None:
        with pytest.raises(ValidationError):
            self._make_agent_card(agent_id)
        with pytest.raises(ValidationError):
            _make_index(agent_id=agent_id)
