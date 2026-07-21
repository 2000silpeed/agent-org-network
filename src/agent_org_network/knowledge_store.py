"""Knowledge Store — 중앙 지식 저장소 포트(본문 보관, Phase 12 S1 잔여, ADR 0033 S1 shape 절).

`PublishedIndexStore`(ADR 0028 — 목차만·라우팅 축)와 나란한 **답변 축**(본문). S3
(`knowledge_sync.py`)가 admission(수용 관문)까지 다뤘던 것을, 이 모듈이 그 결과(수용된
`KnowledgeBundleContent`)를 실제로 보관하는 포트+InMemory 구현으로 이어받는다
(`SessionStore`·`ReevalStore`·`TokenStore` 패턴 N번째 — 새 메커니즘 0).

불변식:
  - 순수 보관(전이 아님) — put/get/is_stale은 도메인 전이가 아니라 상태 그릇 조회다.
  - 더 새 version만 수용 — `KnowledgeIndex.version` staleness 정신 재사용. 같은/더
    오래된 version의 put은 무시(기존 것 유지, 멱등).
  - stale 임계 — `AON_KNOWLEDGE_STALE_SECONDS` 설정값(기본 1800초 = 30분, ADR 0033
    결정 3). 낡음 표식은 답 신뢰 *하향 신호*일 뿐 라우팅 배제가 아니다(미아 없음 보존).
  - 미등록 agent_id — get은 None(본문 없음), is_stale은 True(신선도를 논할 본문
    자체가 없으니 안전측 기본값 = 낡음 취급).
"""

from __future__ import annotations

import os
import threading
from copy import deepcopy
from datetime import datetime
from typing import Annotated, Literal, TYPE_CHECKING, Protocol, TypeAlias, final

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from agent_org_network.agent_card import validate_agent_id_format
from agent_org_network.knowledge_sync import (
    KnowledgeBundleContent,
    KnowledgeDoc,
    KnowledgeSyncAck,
    KnowledgeSyncSpec,
    SyncKnowledge,
    accept_knowledge_sync,
)

if TYPE_CHECKING:
    from agent_org_network.agent_card import AgentCard

DEFAULT_KNOWLEDGE_STALE_SECONDS = 1800


def knowledge_stale_seconds() -> int:
    """`AON_KNOWLEDGE_STALE_SECONDS` 설정값(기본 1800초 = 30분, ADR 0033 결정 3)."""
    raw = (os.environ.get("AON_KNOWLEDGE_STALE_SECONDS") or "").strip()
    return int(raw) if raw else DEFAULT_KNOWLEDGE_STALE_SECONDS


class KnowledgeStore(Protocol):
    """중앙 지식 저장소 포트(ADR 0033 S1 shape 절) — 본문 보관·조회·신선도 판정."""

    def put(self, content: KnowledgeBundleContent) -> None: ...

    def get(self, agent_id: str) -> KnowledgeBundleContent | None: ...

    def is_stale(self, agent_id: str, *, now: datetime, threshold_s: int) -> bool: ...


GroundingKnowledgeInvalidReason: TypeAlias = Literal[
    "type_mismatch",
    "agent_id_mismatch",
    "empty_documents",
    "invalid_document",
]


class _GroundingKnowledgeDto(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    @field_validator("*", mode="after")
    @classmethod
    def _strings_must_be_nonblank(cls, value: object, info: ValidationInfo) -> object:
        del info
        if isinstance(value, str) and not value.strip():
            raise ValueError("문자열 값은 비어 있거나 공백일 수 없습니다.")
        return value


@final
class GroundingKnowledgeFound(_GroundingKnowledgeDto):
    kind: Literal["found"] = "found"
    agent_id: str
    content: KnowledgeBundleContent


@final
class GroundingKnowledgeMissing(_GroundingKnowledgeDto):
    kind: Literal["missing"] = "missing"
    agent_id: str


@final
class GroundingKnowledgeInvalid(_GroundingKnowledgeDto):
    kind: Literal["invalid"] = "invalid"
    agent_id: str
    reason_code: GroundingKnowledgeInvalidReason


GroundingKnowledgeResult: TypeAlias = Annotated[
    GroundingKnowledgeFound | GroundingKnowledgeMissing | GroundingKnowledgeInvalid,
    Field(discriminator="kind"),
]


class GroundingKnowledgeReader(Protocol):
    """중앙 Knowledge Store를 S4의 닫힌 typed 결과로 읽는 포트."""

    def read(self, agent_id: str) -> GroundingKnowledgeResult: ...


@final
class KnowledgeStoreGroundingKnowledgeReader:
    """`KnowledgeStore` 반환을 엄격히 검증하고 canonical 결과로 복사하는 어댑터."""

    def __init__(self, store: KnowledgeStore) -> None:
        self._store = store

    def matches_knowledge_store(self, store: KnowledgeStore) -> bool:
        """composition identity gate를 위한 객체 동일성 seam."""
        return self._store is store

    def read(self, agent_id: str) -> GroundingKnowledgeResult:
        validate_agent_id_format(agent_id)
        # Store 호출 예외는 transient dependency failure로 상위 Answer Source가 다룬다.
        # 여기서 Missing/Invalid로 바꾸면 retryable 경계가 사라진다.
        raw = self._store.get(agent_id)
        if raw is None:
            return GroundingKnowledgeMissing(agent_id=agent_id)
        if type(raw) is not KnowledgeBundleContent:
            return self._invalid(agent_id, "type_mismatch")

        try:
            raw_agent_id = raw.agent_id
            raw_documents = raw.documents
            raw_version = raw.version
            raw_synced_at = raw.synced_at
        except Exception:
            return self._invalid(agent_id, "type_mismatch")

        if type(raw_agent_id) is not str or raw_agent_id != agent_id:
            return self._invalid(agent_id, "agent_id_mismatch")
        if type(raw_documents) is not tuple:
            return self._invalid(agent_id, "type_mismatch")
        if not raw_documents:
            return self._invalid(agent_id, "empty_documents")

        canonical_documents: list[KnowledgeDoc] = []
        seen_paths: set[str] = set()
        for raw_document in raw_documents:
            if type(raw_document) is not KnowledgeDoc:
                return self._invalid(agent_id, "invalid_document")
            try:
                path = raw_document.path
                body = raw_document.body
            except Exception:
                return self._invalid(agent_id, "invalid_document")
            if (
                type(path) is not str
                or not path.strip()
                or type(body) is not str
                or not body.strip()
                or path in seen_paths
            ):
                return self._invalid(agent_id, "invalid_document")
            try:
                canonical_document = KnowledgeDoc.model_validate(
                    {"path": path, "body": body},
                    strict=True,
                )
            except Exception:
                return self._invalid(agent_id, "invalid_document")
            seen_paths.add(path)
            canonical_documents.append(canonical_document)

        try:
            canonical_content = KnowledgeBundleContent.model_validate(
                {
                    "agent_id": raw_agent_id,
                    "documents": tuple(canonical_documents),
                    "version": deepcopy(raw_version),
                    "synced_at": deepcopy(raw_synced_at),
                },
                strict=True,
            )
            return GroundingKnowledgeFound(
                agent_id=agent_id,
                content=canonical_content,
            )
        except Exception:
            return self._invalid(agent_id, "type_mismatch")

    @staticmethod
    def _invalid(
        agent_id: str,
        reason_code: GroundingKnowledgeInvalidReason,
    ) -> GroundingKnowledgeInvalid:
        return GroundingKnowledgeInvalid(
            agent_id=agent_id,
            reason_code=reason_code,
        )


class InMemoryKnowledgeStore:
    """in-memory 지식 저장소 — 더 새 version만 수용, agent_id별 최신 본문 하나만 보관."""

    def __init__(self) -> None:
        self._state: dict[str, KnowledgeBundleContent] = {}
        self._lock = threading.Lock()

    def put(self, content: KnowledgeBundleContent) -> None:
        with self._lock:
            existing = self._state.get(content.agent_id)
            if existing is not None and content.version == existing.version:
                return
            if existing is not None and content.synced_at < existing.synced_at:
                return
            self._state[content.agent_id] = content

    def get(self, agent_id: str) -> KnowledgeBundleContent | None:
        with self._lock:
            return self._state.get(agent_id)

    def is_stale(self, agent_id: str, *, now: datetime, threshold_s: int) -> bool:
        with self._lock:
            content = self._state.get(agent_id)
        if content is None:
            return True
        elapsed = (now - content.synced_at).total_seconds()
        return elapsed > threshold_s


def accept_and_store_knowledge_sync(
    session_owner_id: str,
    frame: SyncKnowledge,
    card: "AgentCard",
    spec: KnowledgeSyncSpec,
    store: KnowledgeStore,
) -> KnowledgeSyncAck:
    """S3 admission(`accept_knowledge_sync`)과 S1 잔여 보관(`KnowledgeStore`)을 잇는다.

    `accept_knowledge_sync`(knowledge_sync.py) 시그니처는 그대로 두고(admission
    *판정*만 순수 담당), 이 조합 함수가 호출측에서 판정 결과가 `Admitted`일 때만
    `store.put(content)`을 이어붙인다(전이≠기록 — 판정은 도메인 결정, 보관은 별
    축이라는 두 모듈의 경계를 조합 지점 하나로 접합).

    반환값은 `accept_knowledge_sync`가 낸 `KnowledgeSyncAck` 그대로(보관 성공 여부로
    응답을 바꾸지 않는다 — admission 판정이 곧 워커에게 알릴 진실이다).

    `result.accepted`가 True라는 것은 admission이 `Admitted(content=frame.content)`였다는
    뜻(`admit_knowledge`는 content를 변형하지 않고 그대로 감싸 반환)이므로, 저장할
    본문은 `frame.content` 그대로다 — admission을 재실행하지 않는다(이중 계산 0).
    """
    result = accept_knowledge_sync(session_owner_id, frame, card, spec)
    if result.accepted:
        store.put(frame.content)
    return result


__all__ = [
    "DEFAULT_KNOWLEDGE_STALE_SECONDS",
    "knowledge_stale_seconds",
    "KnowledgeStore",
    "GroundingKnowledgeInvalidReason",
    "GroundingKnowledgeFound",
    "GroundingKnowledgeMissing",
    "GroundingKnowledgeInvalid",
    "GroundingKnowledgeResult",
    "GroundingKnowledgeReader",
    "KnowledgeStoreGroundingKnowledgeReader",
    "InMemoryKnowledgeStore",
    "accept_and_store_knowledge_sync",
]
