"""KnowledgeIndex·Concept 값 객체 (ADR 0028 §4).

각 owner가 자기 지식의 경량 *목차*(내용 아님)를 중앙에 publish할 때 사용하는
frozen pydantic v2 값 객체. AgentCard admission 정신 그대로 재사용.

agent_id 검증 규칙: agent_card.validate_agent_id_format(공유 헬퍼)를 호출해 AgentCard와
동일 정책을 적용한다 — 중복 정의 없이 단일 권위 소스 공유(ADR 0023).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator, model_validator

from agent_org_network.agent_card import validate_agent_id_format


class Concept(BaseModel, frozen=True):
    """owner 지식의 단일 개념 항목 — 내용이 아닌 *목차 한 줄*.

    core_question이 라우팅 키다: "이 개념이 어떤 질문에 답하는가"를 담아
    stage-1 개념 오버랩 매칭에 쓰인다.

    domain: 이 개념이 속한 owner의 owned domain(권한 술어·intent 매핑의 링크).
      - T10.3a admission 재검증에서 `concept.domain in card.domains` 로 over-claim 차단.
      - `RoutingDecision.intent = concept.domain`(결정 E·ADR 0015 정합).
      - 값 자체의 카드 owned-domains 일치 검증은 라우팅·publish 수용 시에 함 —
        Concept 생성(값 객체)에서는 형식 검증(빈/공백 거부)만 한다.
    """

    id: str
    label: str
    core_question: str
    domain: str
    type: str | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Concept.id는 빈 문자열/공백이 될 수 없습니다.")
        return value

    @field_validator("core_question")
    @classmethod
    def _validate_core_question(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Concept.core_question은 빈 문자열/공백이 될 수 없습니다.")
        return value

    @field_validator("domain")
    @classmethod
    def _validate_domain(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Concept.domain은 빈 문자열/공백이 될 수 없습니다.")
        return value


class ConceptEdge(BaseModel, frozen=True):
    """개념 간 관계 — MVP 죽은 필드(후속 계층 좁히기용). 최소 모양만."""

    from_id: str
    to_id: str
    relation: str


class KnowledgeIndex(BaseModel, frozen=True):
    """owner가 중앙에 publish하는 경량 지식 목차.

    agent_id: AgentCard.agent_id와 동일 wire-format admission(ADR 0023 공유).
    version: staleness 판정용 — 빈 문자열 거부.
    concepts: 목차 항목 집합 — 빈 튜플 허용(개념 없음·0 후보로 자연 처리).
    edges: MVP 죽은 필드 — 기본 빈 튜플.
    """

    agent_id: str
    version: str
    generated_at: datetime
    concepts: tuple[Concept, ...]
    edges: tuple[ConceptEdge, ...] = ()

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, value: str) -> str:
        return validate_agent_id_format(value)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        if not value:
            raise ValueError("version은 빈 문자열이 될 수 없습니다.")
        return value

    @model_validator(mode="after")
    def _validate_concept_ids_unique(self) -> KnowledgeIndex:
        ids = [c.id for c in self.concepts]
        if len(ids) != len(set(ids)):
            raise ValueError("concepts 내 concept.id는 유일해야 합니다(중복 거부).")
        return self
