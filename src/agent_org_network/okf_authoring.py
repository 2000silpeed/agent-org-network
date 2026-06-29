"""OKF Authoring 값 객체 — T11.1 (ADR 0029 S1·경로 위생은 ADR 0028 §15).
T11.2 — OkfAuthor 포트·FakeAuthor·staged 오케스트레이터·실 어댑터는 T11.7 게이트 밖.

owner가 OKF 초안을 작성할 때 쓰는 frozen pydantic v2 값 객체.
SDK 0·IO 0·결정론. 권한(domain∈card.domains) 검증은 하지 않는다(ADR 0004·형식만).

agent_card.py: validate_agent_id_format·validate_safe_path_component 공유 헬퍼 재사용.
knowledge_index.py: ConceptEdge 재사용(새 edge 타입 정의 금지).

concept_id admission은 소비측 okf_index 도출(파일 stem=concept.id·glob)보다 *더 엄격한
superset*이다 — okf_index는 파일시스템이 stem을 보증해 별도 거부를 안 하지만, 저작은
구분자·`..`·절대경로를 능동 거부한다(저작 산출 ⊆ okf_index 수용 집합·안전 방향). T11.4
합류 시 이 비대칭을 결함으로 오해하지 말 것.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, field_validator, model_validator

from agent_org_network.agent_card import validate_agent_id_format, validate_safe_path_component
from agent_org_network.knowledge_index import ConceptEdge


class RawSource(BaseModel, frozen=True):
    """owner가 제공하는 원본 소스 텍스트 — concept 추출의 입력 단위.

    source_id: owner 내부 식별자(파일/중앙 미도달) — path sanitization 적용 안 함.
    content: 비어 있으면 추출 불가이므로 거부.
    """

    source_id: str
    content: str

    @field_validator("source_id")
    @classmethod
    def _validate_source_id(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("RawSource.source_id는 빈 문자열/공백이 될 수 없습니다.")
        return value

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("RawSource.content는 빈 문자열/공백이 될 수 없습니다.")
        return value


class OkfDocumentDraft(BaseModel, frozen=True):
    """OKF 문서 초안 — concept 한 건의 작성 단위.

    concept_id: OKF 파일 stem이 되므로 path-traversal 화이트리스트 적용
                (validate_safe_path_component — agent_card.py 단일 권위).
    domain: 형식 검증만. 권한(domain∈card.domains) 검증은 라우팅/publish 수용 시
            중앙에서 한다(ADR 0004 — 자기보고 금지·Authority 중앙).
    type: 무검증·선택 필드.
    """

    concept_id: str
    title: str
    body: str
    core_question: str
    domain: str
    type: str | None = None

    @field_validator("concept_id")
    @classmethod
    def _validate_concept_id(cls, value: str) -> str:
        return validate_safe_path_component(value)

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("OkfDocumentDraft.title은 빈 문자열/공백이 될 수 없습니다.")
        return value

    @field_validator("body")
    @classmethod
    def _validate_body(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("OkfDocumentDraft.body는 빈 문자열/공백이 될 수 없습니다.")
        return value

    @field_validator("core_question")
    @classmethod
    def _validate_core_question(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("OkfDocumentDraft.core_question은 빈 문자열/공백이 될 수 없습니다.")
        return value

    @field_validator("domain")
    @classmethod
    def _validate_domain(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("OkfDocumentDraft.domain은 빈 문자열/공백이 될 수 없습니다.")
        return value


class OkfDraft(BaseModel, frozen=True):
    """OKF 초안 번들 — 한 agent의 문서 집합과 개념 간 관계.

    agent_id: validate_agent_id_format 공유 헬퍼(ADR 0023 — agent_card.py 단일 권위).
    documents: 중복 concept_id 거부. 빈 튜플 허용.
    edges: ConceptEdge 재사용(knowledge_index.py). 기본 빈 튜플.
           dangling edge(documents에 없는 concept)는 거부하지 않는다 — T11.3 책임.
    """

    agent_id: str
    documents: tuple[OkfDocumentDraft, ...]
    edges: tuple[ConceptEdge, ...] = ()

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, value: str) -> str:
        return validate_agent_id_format(value)

    @model_validator(mode="after")
    def _validate_no_duplicate_concept_id(self) -> OkfDraft:
        ids = [doc.concept_id for doc in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("OkfDraft.documents 내 concept_id는 유일해야 합니다(중복 거부).")
        return self


# ── T11.2: OkfAuthor 포트·AuthoredOkf·FakeAuthor·staged 오케스트레이터 ────────


class OkfAuthor(Protocol):
    """staged OKF 저작 포트 — Classifier·KnowledgeIndexMatcher 포트와 동일 패턴.

    단계 분리: split(2단계 개념 분할) → derive_core_questions(3단계 core_question 정련)
               → link(4단계 edges). one-shot 단일 메서드 두지 않는다(T11.2 설계).

    구현: FakeAuthor(테스트)·실 어댑터는 T11.7 게이트 밖.
    """

    def split(self, sources: Sequence[RawSource]) -> tuple[OkfDocumentDraft, ...]:
        """2단계: 원본 소스를 개념 단위로 분할한다."""
        ...

    def derive_core_questions(
        self, drafts: Sequence[OkfDocumentDraft]
    ) -> tuple[OkfDocumentDraft, ...]:
        """3단계: 각 개념 초안의 core_question을 정련한다."""
        ...

    def link(
        self, drafts: Sequence[OkfDocumentDraft]
    ) -> tuple[ConceptEdge, ...]:
        """4단계: 개념 간 관계(edges)를 도출한다."""
        ...


class AuthoredOkf(BaseModel, frozen=True):
    """staged 파이프라인 산출 래퍼 — OkfDraft + 원본 RawSource 보존.

    sources: T11.5 증분 diff seam — 어느 raw가 어느 concept 낳았나 추적 자리.
             지금은 보존만·매핑 0.
    """

    draft: OkfDraft
    sources: tuple[RawSource, ...]


class FakeAuthor:
    """OkfAuthor Protocol 테스트 더블 — FakeMatcher·StubRuntime 정신.

    생성 시 주입한 고정 산출을 입력 무관하게 그대로 반환한다.
    관측 seam(split_seen·derive_seen·link_seen)으로 staged 순차 흐름·데이터 배선을 단언한다.
    """

    def __init__(
        self,
        split_result: tuple[OkfDocumentDraft, ...],
        derive_result: tuple[OkfDocumentDraft, ...],
        link_result: tuple[ConceptEdge, ...],
    ) -> None:
        self._split_result = split_result
        self._derive_result = derive_result
        self._link_result = link_result
        self.split_seen: tuple[RawSource, ...] | None = None
        self.derive_seen: tuple[OkfDocumentDraft, ...] | None = None
        self.link_seen: tuple[OkfDocumentDraft, ...] | None = None

    def split(self, sources: Sequence[RawSource]) -> tuple[OkfDocumentDraft, ...]:
        """입력 무관 고정 산출 반환·split_seen에 입력 기록."""
        self.split_seen = tuple(sources)
        return self._split_result

    def derive_core_questions(
        self, drafts: Sequence[OkfDocumentDraft]
    ) -> tuple[OkfDocumentDraft, ...]:
        """입력 무관 고정 산출 반환·derive_seen에 입력 기록."""
        self.derive_seen = tuple(drafts)
        return self._derive_result

    def link(
        self, drafts: Sequence[OkfDocumentDraft]
    ) -> tuple[ConceptEdge, ...]:
        """입력 무관 고정 산출 반환·link_seen에 입력 기록."""
        self.link_seen = tuple(drafts)
        return self._link_result


def run_authoring_pipeline(
    agent_id: str,
    sources: Sequence[RawSource],
    author: OkfAuthor,
) -> AuthoredOkf:
    """staged OKF 저작 오케스트레이터 — 순수 함수.

    1단계(인제스트)는 sources 인자로 이미 받았다고 전제(T11.6 Ingestor seam).
    5단계(인덱싱)는 하지 않는다(T11.4 seam — KnowledgeIndex 미선취).
    HITL 승인 상태는 부여하지 않는다(T11.3 seam).
    """
    split_drafts = author.split(sources)            # 2단계: 개념 분할
    derived = author.derive_core_questions(split_drafts)  # 3단계: core_question 정련
    edges = author.link(derived)                    # 4단계: edges 도출
    draft = OkfDraft(agent_id=agent_id, documents=derived, edges=edges)
    return AuthoredOkf(draft=draft, sources=tuple(sources))
