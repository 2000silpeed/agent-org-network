"""Knowledge Sync — 워커→중앙 지식 동기화 채널의 결정론 코어 (Phase 12 S3, ADR 0033 결정 3).

ADR 0033 S1 shape 절의 `KnowledgeSyncSpec`·`KnowledgeDoc`·`KnowledgeBundleContent`
값 객체와, "명시 지정 + 민감 필터" admission(`filter_sensitive`·`admit_knowledge`)을
담는다. `KnowledgeStore` 포트 자체(본문 보관소)는 S2 몫으로 남긴다 — 이 모듈은 동기화
*수용 관문*(무엇을 들여보낼지)까지만 다룬다(tasks-v0.md S1 잔여 메모 참조).

이 모듈이 두 번째로 담는 것은 워커→중앙 WS 프레임(`SyncKnowledge`)과 중앙→워커 응답
(`KnowledgeSyncAck`) DTO, 그리고 프레임 수신 시 admission을 거쳐 수용/거부를 판정하는
순수 함수(`accept_knowledge_sync`)다. 실 WS 소켓 배선·실 파일 읽기는 게이트 밖
(mcp-runtime-engineer 몫) — 여기서는 프레임 직렬화/역직렬화 + 수용 로직만 결정론으로 다룬다.

불변식:
  - 명시 지정만 동기화 — `KnowledgeSyncSpec.paths` 밖의 경로는 거부(owner 전체를
    빨아들이지 않음, ADR 0033 외부 결정 ①).
  - 민감정보 이중 방어 — 패턴 필터(1차, `filter_sensitive`) + 지정 책임(2차, 정직한 한계).
  - Authority 중앙 — over-claim 필터(`domain_authorized` 재사용)로 권한 자기보고가
    admission을 못 넓힌다(ADR 0028 정신 재사용).
  - 유효하지 않은 지식은 수용되지 않는다(등록 무결성 — 카드 admission "무효 카드 등록
    금지"와 같은 사상).
  - 전이 ≠ 기록 — admission은 순수 판정이지 보관(그 자체)이 아니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from agent_org_network.agent_card import validate_agent_id_format

if TYPE_CHECKING:
    from agent_org_network.agent_card import AgentCard


# ── 값 객체(ADR 0033 S1 shape 절) ────────────────────────────────────────────


class KnowledgeDoc(BaseModel, frozen=True):
    """동기화되는 본문 단위 — 지정 경로(agent_id 상대) + 본문.

    `body`는 admission(민감 필터 통과분)만 `KnowledgeBundleContent`에 실린다 — 이
    값 객체 자체는 필터 *이전* 원본도 담을 수 있다(admission 입력이므로).
    """

    path: str
    body: str

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("KnowledgeDoc.path는 빈 문자열/공백이 될 수 없습니다.")
        return value


class KnowledgeBundleContent(BaseModel, frozen=True):
    """동기화된 본문 단위 — agent_id별 명시 지정 문서 묶음(ADR 0033 S1 shape 절).

    `KnowledgeStore`(본문 보관 포트) 자체는 S2 몫이라 이 모듈에 두지 않는다 — 이
    값 객체는 그 포트의 `put()` 인자 모양으로 여기서 확정해 둔다.
    """

    agent_id: str
    documents: tuple[KnowledgeDoc, ...]
    version: str
    synced_at: datetime

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


class KnowledgeSyncSpec(BaseModel, frozen=True):
    """owner가 명시 지정하는 동기화 경계(ADR 0033 외부 결정 ①) — 이것만 동기화한다."""

    agent_id: str
    paths: tuple[str, ...]

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, value: str) -> str:
        return validate_agent_id_format(value)


# ── 민감정보 패턴 필터(admission 1차 방어, ADR 0033 결정 3) ──────────────────


@dataclass(frozen=True)
class Clean:
    """민감정보 패턴 미검출 — 본문이 통과."""


@dataclass(frozen=True)
class Blocked:
    """민감정보 패턴 검출 — 차단(사유: 걸린 패턴 이름들)."""

    patterns: tuple[str, ...]
    reason: str = ""


SensitivityVerdict = Clean | Blocked


# 주민등록번호: 6자리-7자리(둘째자리 그룹 1~8 시작 — 내국인 1~4·외국인/2000년대 이후 5~8).
_RRN_RE = re.compile(r"\b\d{6}[-\s]?[1-8]\d{6}\b")
# API 키/시크릿류 — 흔한 공급자 프리픽스(sk-, AKIA 등) + 긴 토큰 통칭 패턴.
# sk- 계열은 `sk-proj-...`처럼 프리픽스 뒤에 하이픈이 섞인 신형 키가 있어 `-`도
# 토큰 문자에 포함한다 — 다만 "risk-assessment-..." 같은 일반 하이픈 복합어를
# 오탐하지 않도록 `sk-` 리터럴 프리픽스(단어 경계 `\b`) 뒤에서만 매치한다.
_API_KEY_RE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{16,}|(?i:akia)[0-9A-Za-z]{16}|ghp_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,})"
)
# 비밀번호류 — "password/passwd/비밀번호/secret" 키 뒤 값 대입(공백/구두점 관대).
_PASSWORD_RE = re.compile(
    r"(?i)\b(?:password|passwd|secret|비밀번호)\s*[:=]\s*\S+"
)

_PATTERN_TABLE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("resident_registration_number", _RRN_RE),
    ("api_key", _API_KEY_RE),
    ("password", _PASSWORD_RE),
)


def filter_sensitive(body: str) -> SensitivityVerdict:
    """민감정보 패턴 필터 — 순수 함수(ADR 0033 결정 3 1차 방어).

    주민등록번호·API 키/시크릿·비밀번호류 패턴을 검사한다. 하나라도 검출되면
    `Blocked(patterns=...)`(검출된 전 패턴 이름을 담아 사유 보존). 전부 미검출이면
    `Clean()`. 지정 책임(2차 방어)은 이 함수 밖(owner가 지정한 경로 자체의 책임) —
    이 함수는 실수 방어망이지 완전 보장이 아니다(정직한 한계, ADR 0033 결정 3).
    """
    hit_names = tuple(name for name, pattern in _PATTERN_TABLE if pattern.search(body))
    if hit_names:
        return Blocked(patterns=hit_names, reason=f"민감정보 패턴 검출: {', '.join(hit_names)}")
    return Clean()


# ── 동기화 admission(ADR 0033 S1 shape 절) ───────────────────────────────────


@dataclass(frozen=True)
class Admitted:
    """동기화 수용 — 지정 경계 통과 + 민감 필터 통과."""

    content: KnowledgeBundleContent


@dataclass(frozen=True)
class Rejected:
    """동기화 거부 — 사유 보존(지정 밖 경로/민감정보 검출/미등록 agent_id 등)."""

    reason: str


AdmissionResult = Admitted | Rejected


def _path_in_spec(path: str, spec: KnowledgeSyncSpec) -> bool:
    """path가 spec.paths 중 하나와 일치하거나 그 하위인가(디렉터리 지정 허용)."""
    for allowed in spec.paths:
        if path == allowed or path.startswith(allowed.rstrip("/") + "/"):
            return True
    return False


def admit_knowledge(
    content: KnowledgeBundleContent,
    card: "AgentCard",
    spec: KnowledgeSyncSpec,
) -> AdmissionResult:
    """동기화 admission — 순수 함수(ADR 0033 S1 shape 절).

    순서: ① agent_id가 spec/card와 일치하는가(스코핑 — 등록 무결성) ② 문서마다
    지정 경계(spec.paths) 안인가(외부 결정 ① — 명시 지정만) ③ 문서마다 민감정보
    패턴 필터 통과인가(외부 결정 ② 1차 방어). 하나라도 위반이면 전체 거부
    (`Rejected(reason=...)`) — 부분 수용하지 않는다(등록 무결성: "유효하지 않은
    지식은 수용되지 않는다"를 번들 단위로 강제). 전부 통과하면 `Admitted(content)`.

    Authority 중앙 — `KnowledgeDoc`은 concept/domain을 안 가지므로(본문 단위이지
    개념 단위가 아님) `domain_authorized` 자체는 재사용하지 않는다. 대신 그 함수가
    강제하는 것과 *같은 사상*(권한 자기보고 차단)을 agent_id 스코핑으로 강제한다 —
    content.agent_id가 spec·card의 agent_id와 정확히 일치해야만 수용되어, 동기화가
    다른 owner/agent_id로 못 새게 막는다(ADR 0028 over-claim 필터의 정신 재사용).
    """
    if content.agent_id != spec.agent_id:
        return Rejected(
            reason=f"agent_id 불일치: content={content.agent_id!r} spec={spec.agent_id!r}"
        )
    if content.agent_id != card.agent_id:
        return Rejected(
            reason=f"agent_id 불일치: content={content.agent_id!r} card={card.agent_id!r}"
        )
    for doc in content.documents:
        if not _path_in_spec(doc.path, spec):
            return Rejected(reason=f"지정 밖 경로: {doc.path!r}(spec.paths={spec.paths!r})")
    for doc in content.documents:
        verdict = filter_sensitive(doc.body)
        if isinstance(verdict, Blocked):
            return Rejected(reason=f"{doc.path!r}: {verdict.reason}")
    return Admitted(content=content)


# ── WS 프레임 DTO(워커→중앙·중앙→워커) ───────────────────────────────────────


class _Frame(BaseModel):
    """전송 프레임 공통 베이스 — `transport._Frame`과 같은 정신(frozen·미지 필드 거부)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class SyncKnowledge(_Frame):
    """워커→중앙 지식 동기화 업스트림 프레임(ADR 0033 결정 3·`PublishIndex` 채널 재사용 정신).

    `PublishIndex`가 `KnowledgeIndex`(목차)를 통째로 싣듯 이 프레임은
    `KnowledgeBundleContent`(본문)를 통째로 싣는다. owner는 프레임에 다시 싣지 않는다
    — 연결 세션의 인증 owner가 스코핑 기준(`PublishIndex.publishable` 정신 재사용).
    """

    type: Literal["sync_knowledge"] = "sync_knowledge"
    content: KnowledgeBundleContent


class KnowledgeSyncAck(_Frame):
    """중앙→워커 수용/거부 응답 다운스트림 프레임 — admission 결과를 워커에 알린다.

    `accepted=False`면 `reason`에 거부 사유가 담긴다(`Rejected.reason` 그대로 보존
    — 워커가 "왜 거부됐는지" 알 수 있어야 재시도/수정 판단이 가능하다).
    """

    type: Literal["knowledge_sync_ack"] = "knowledge_sync_ack"
    agent_id: str
    accepted: bool
    reason: str = ""


def accept_knowledge_sync(
    session_owner_id: str,
    frame: SyncKnowledge,
    card: "AgentCard",
    spec: KnowledgeSyncSpec,
) -> KnowledgeSyncAck:
    """중앙이 `SyncKnowledge` 한 건을 수용 처리한다 — 스코핑→admission→응답(결정론 코어).

    순서: ① 워커-소유자 스코핑(연결 세션의 인증 owner == card.owner) — 불일치면 거부
    (`PublishIndex.publishable` 정신 재사용, 사칭/미등록 차단). ② `admit_knowledge`
    (지정 경계 + 민감 필터). 실 `KnowledgeStore.put()` 호출(보관)은 이 함수 밖(S2/S3
    실 배선 몫) — 이 함수는 수용 여부 *판정*과 그 판정을 담은 응답 프레임 생성까지만
    한다(전이 ≠ 기록: 판정은 도메인 결정, 보관은 별 축).
    """
    if card.owner != session_owner_id:
        return KnowledgeSyncAck(
            agent_id=frame.content.agent_id,
            accepted=False,
            reason=f"워커-소유자 스코핑 실패: session_owner={session_owner_id!r} "
            f"card.owner={card.owner!r}",
        )
    result = admit_knowledge(frame.content, card, spec)
    if isinstance(result, Admitted):
        return KnowledgeSyncAck(agent_id=frame.content.agent_id, accepted=True)
    return KnowledgeSyncAck(
        agent_id=frame.content.agent_id, accepted=False, reason=result.reason
    )


__all__ = [
    "KnowledgeDoc",
    "KnowledgeBundleContent",
    "KnowledgeSyncSpec",
    "Clean",
    "Blocked",
    "SensitivityVerdict",
    "filter_sensitive",
    "Admitted",
    "Rejected",
    "AdmissionResult",
    "admit_knowledge",
    "SyncKnowledge",
    "KnowledgeSyncAck",
    "accept_knowledge_sync",
]
