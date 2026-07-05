"""관리 UI 도메인 — 라이브 카드 등록 + 오너 변경 전이(Phase 12 3라운드·ADR 0034).

이 모듈은 관리 UI의 *얇은 도메인 코어*다 — 폼→카드 후보를 admission 관문에 그대로
태우고(우회 API 금지·ADR 0034 결정 1·ADR 0023 계승), 통과 시 라이브 Registry에
즉시 반영한다(YAML은 초기 시드로 강등). 오너 변경은 재-admission + 구 owner 워커
토큰 revoke를 *같은 임계 구역*에서 강제하는 전이다(ADR 0034 결정 2·owner 격리).

핵심 계약:
  - **무효 카드 등록 금지**: 신규 등록·오너 변경 둘 다 `admit_card`(형식 + 참조
    무결성) 관문을 통과해야 라이브에 들어간다. 관문을 건너뛰는 우회 경로 없음.
  - **토큰 revoke 원자성**: 오너 변경은 (1) 스위치(frozen 값 교체)와 (2) 구 owner
    토큰 revoke를 같은 임계 구역(`_lock`)에서 실행한다 — revoke가 스위치보다 늦으면
    그 window 동안 구 owner가 verify를 통과해 새 owner 카드로 회신할 수 있다(owner
    격리 붕괴). 이건 편의가 아니라 보안 불변식이다.
  - **전이 ≠ 기록**: 전이(스위치)는 도메인, `OwnershipTransfer` 감사 이벤트는 기록.
    둘을 분리해 append-only 감사 로그(`action_record`)에 남긴다.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Protocol, cast

from agent_org_network.agent_card import AgentCard

if TYPE_CHECKING:
    from agent_org_network.registry import Registry
    from agent_org_network.sqlite_stores import SqliteRegistryJournal
    from agent_org_network.token import AdmissionToken, TokenStore


Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _empty_str_list() -> list[str]:
    return []


@dataclass(frozen=True)
class CardCandidate:
    """폼→카드 후보 DTO — admission 관문에 넘길 원시 필드(얇은 어댑터 입력).

    `AgentCard` 필드를 미러하되 `last_reviewed_at`은 ISO date 문자열로 받는다
    (pydantic이 date로 강제·`BuilderValidateRequest`와 같은 관례). 권한류 필드
    (`can_answer` 등)를 받아도 그건 카드 under-claim 자기보고일 뿐(ADR 0004) —
    Authority SSOT는 여전히 중앙(`routing_rules.yaml`).
    """

    agent_id: str
    owner: str
    team: str
    summary: str
    domains: list[str]
    last_reviewed_at: str
    maintainer: str | None = None
    can_answer: list[str] = field(default_factory=_empty_str_list)
    cannot_answer: list[str] = field(default_factory=_empty_str_list)
    approval_when: list[str] = field(default_factory=_empty_str_list)
    collaborate_when: list[str] = field(default_factory=_empty_str_list)
    knowledge_sources: list[str] = field(default_factory=_empty_str_list)
    trust_labels: list[str] = field(default_factory=_empty_str_list)


def admit_card(
    candidate: CardCandidate, registry: "Registry"
) -> tuple[AgentCard | None, list[str]]:
    """카드 후보를 admission 규칙으로 검증해 `(AgentCard|None, errors)`를 낸다(순수 함수).

    관문(ADR 0023 계승·`validate_card_for_builder`와 같은 결):
      ① 빈 agent_id 거부.
      ② `AgentCard.model_validate`(필수 필드·타입·agent_id wire-format·date 파싱).
      ③ 참조 무결성 — `card.owner`가 Registry 실재 User, maintainer 있으면 그것도.
    통과면 `(card, [])`, 실패면 `(None, [사유...])`. 라이브 mutation은 *하지 않는다*
    (이 함수는 판정만 — 반영은 `register_card`/`transfer_ownership`이 진다).
    """
    from pydantic import ValidationError

    if not candidate.agent_id or not candidate.agent_id.strip():
        return None, ["agent_id는 비어 있을 수 없습니다."]

    raw: dict[str, Any] = {
        "agent_id": candidate.agent_id,
        "owner": candidate.owner,
        "team": candidate.team,
        "summary": candidate.summary,
        "domains": list(candidate.domains),
        "last_reviewed_at": candidate.last_reviewed_at,
        "maintainer": candidate.maintainer,
        "can_answer": list(candidate.can_answer),
        "cannot_answer": list(candidate.cannot_answer),
        "approval_when": list(candidate.approval_when),
        "collaborate_when": list(candidate.collaborate_when),
        "knowledge_sources": list(candidate.knowledge_sources),
        "trust_labels": list(candidate.trust_labels),
    }
    try:
        card = AgentCard.model_validate(raw)
    except ValidationError as exc:
        return None, [str(e["msg"]) for e in exc.errors()]

    errors: list[str] = []
    if card.owner not in registry.user_ids():
        errors.append(f"미등록 owner: {card.owner}")
    if card.maintainer is not None and card.maintainer not in registry.user_ids():
        errors.append(f"미등록 maintainer: {card.maintainer}")
    if errors:
        return None, errors
    return card, []


class AdmissionError(Exception):
    """admission 관문 실패 — 무효 카드는 라이브에 들어가지 않는다(불변식).

    `errors`는 사유 목록(폼에 그대로 표시). 웹 어댑터가 422로 매핑한다.
    """

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class DuplicateCardError(Exception):
    """이미 존재하는 agent_id로 신규 등록 시도 — 등록 무결성(중복 거부)."""


class UnknownCardError(Exception):
    """전이 대상 agent_id가 Registry에 없음 — 오너 변경 대상 부재."""


@dataclass(frozen=True)
class OwnershipTransferResult:
    """오너 변경 전이 결과 — 스위치된 카드 + revoke된 구 owner 토큰 id들 + 감사 인덱스."""

    agent_id: str
    from_owner: str
    to_owner: str
    revoked_token_ids: list[str]
    audit_index: int


class AdminAuditSink(Protocol):
    """관리 이벤트를 append-only 감사 로그에 남기는 포트(`AuditLog.record_action` 정신)."""

    def record_action(self, record: dict[str, Any]) -> None: ...


class RegistryJournalSink(Protocol):
    """카드 라이브 등록·오너 변경을 durable 저널에 남기는 포트(ADR 0034 결정 1·2).

    `SqliteRegistryJournal`(`sqlite_stores.py`)이 실 구현. `AdminAuditSink`와
    다른 축이다 — 감사 로그는 *사람이 읽는 이력*(who/what/when), 저널은
    *기계가 재생하는 리플레이 원천*(재기동 시 라이브 Registry 복원). 둘을
    분리해 각자의 목적에 맞는 포맷·소비자를 유지한다.
    """

    def append_register(
        self,
        *,
        agent_id: str,
        owner: str,
        team: str,
        summary: str,
        domains: list[str],
        last_reviewed_at: str,
        by: str,
        at: datetime,
        maintainer: str | None = None,
        can_answer: list[str] = ...,
        cannot_answer: list[str] = ...,
        approval_when: list[str] = ...,
        collaborate_when: list[str] = ...,
        knowledge_sources: list[str] = ...,
        trust_labels: list[str] = ...,
    ) -> None: ...

    def append_transfer(
        self,
        *,
        agent_id: str,
        owner: str,
        team: str,
        summary: str,
        domains: list[str],
        last_reviewed_at: str,
        by: str,
        at: datetime,
        maintainer: str | None = None,
        can_answer: list[str] = ...,
        cannot_answer: list[str] = ...,
        approval_when: list[str] = ...,
        collaborate_when: list[str] = ...,
        knowledge_sources: list[str] = ...,
        trust_labels: list[str] = ...,
    ) -> None: ...


class AdminRegistryService:
    """라이브 카드 등록 + 오너 변경 전이 서비스(ADR 0034 결정 1·2).

    같은 `Registry` 인스턴스를 Router·AskOrg가 라이브로 읽으므로(라우터는 매 route마다
    `registry.all_cards()` — 재색인 불요), 여기서 등록·전이가 반영되면 *다음 라우팅*에
    즉시 잡힌다. `token_store`가 주입되면 오너 변경 시 구 owner의 활성 토큰을 revoke하고,
    `disconnect_owner`가 주입되면 구 owner 워커 WS 세션을 끊는다(presence offline로 귀결).
    """

    def __init__(
        self,
        registry: "Registry",
        *,
        audit_sink: AdminAuditSink | None = None,
        token_store: "TokenStore | None" = None,
        disconnect_owner: Callable[[str], None] | None = None,
        journal_sink: "RegistryJournalSink | None" = None,
        clock: Clock = _default_clock,
    ) -> None:
        self._registry = registry
        self._audit_sink = audit_sink
        self._token_store = token_store
        self._disconnect_owner = disconnect_owner
        self._journal_sink = journal_sink
        self._clock = clock
        # 스위치 + 토큰 revoke를 원자적으로 묶는 임계 구역(ADR 0034 결정 2).
        self._lock = threading.RLock()

    def register_card(self, candidate: CardCandidate, *, by: str) -> AgentCard:
        """신규 카드를 admission 통과 즉시 라이브 Registry에 반영한다(ADR 0034 결정 1).

        흐름: admission 검증(무효면 AdmissionError) → 라이브 `registry.register`(중복이면
        DuplicateCardError) → 감사 로그 append (+ `journal_sink` 주입 시 durable 저널
        append, ADR 0034 결정 1 "AON_DB 영속"). YAML은 초기 시드일 뿐 여기서 파일을 쓰지
        않는다(git 추적은 감사 로그가 대신). `by`는 등록을 낸 운영자 신원(감사 who).
        """
        from agent_org_network.registry import RegistryError

        card, errors = admit_card(candidate, self._registry)
        if card is None:
            raise AdmissionError(errors)

        with self._lock:
            if self._registry.has_card(card.agent_id):
                raise DuplicateCardError(f"이미 존재하는 agent_id: {card.agent_id}")
            try:
                self._registry.register(card)
            except RegistryError as exc:  # 동시 등록 경합(TOCTOU) 방어
                raise DuplicateCardError(str(exc)) from exc

        if self._journal_sink is not None:
            self._journal_sink.append_register(**_journal_kwargs(candidate, by=by, at=self._clock()))

        self._record_action(
            action="CardRegistered",
            subject_id=card.agent_id,
            by=by,
            detail={"owner": card.owner, "team": card.team},
        )
        return card

    def transfer_ownership(
        self, candidate: CardCandidate, *, by: str
    ) -> OwnershipTransferResult:
        """오너 변경 전이 — 재-admission + 스위치 + 구 owner 토큰 revoke(원자·ADR 0034 결정 2).

        순서(전이 ≠ 기록):
          1. 재-admission 검증(agent_id 형식·새 owner 실재·참조 무결성). 무효면 스위치 없음.
          2. **임계 구역**: (a) 스위치(frozen 값 교체 — agent_id 불변·owner A→B),
             (b) 같은 구역에서 구 owner A의 활성 토큰 전부 revoke(owner 격리 — revoke가
             스위치보다 늦으면 그 window에 구 owner가 새 owner 카드로 회신 가능).
          3. 구 owner 워커 WS 세션 끊기(presence offline로 귀결 — 임계 구역 밖·부작용).
          4. `OwnershipTransfer` 감사 이벤트 append(who: 운영자, what: agent_id·from·to).

        새 owner가 구 owner와 같으면(no-op 전이) UnknownCardError가 아니라 그대로 스위치·
        revoke는 자기 토큰이 대상이 되므로 사실상 자기 워커를 끊는다 — 호출측(웹)이 동일
        owner를 거르는 게 자연스러우나, 도메인은 방어적으로 진행하지 않고 그대로 전이한다.
        """
        card, errors = admit_card(candidate, self._registry)
        if card is None:
            raise AdmissionError(errors)

        # 전이 대상 부재는 임계 구역 밖에서 먼저 거른다(재-admission은 이미 통과).
        if not self._registry.has_card(card.agent_id):
            raise UnknownCardError(f"미존재 agent_id: {card.agent_id}")

        with self._lock:
            old_card = self._registry.get(card.agent_id)
            from_owner = old_card.owner
            to_owner = card.owner
            # (a) 스위치 — frozen 값 교체(agent_id 불변).
            self._registry.replace_card(card)
            # (b) 같은 임계 구역에서 구 owner 토큰 revoke(owner 격리 보안 계약).
            revoked_token_ids = self._revoke_owner_tokens(from_owner)

        # 구 owner 워커 WS 세션 끊기(부작용 — 임계 구역 밖). 이미 열린 세션은 토큰
        # revoke만으로는 안 닫히므로(revoke는 이후 verify만 막음) 명시적 disconnect로
        # 즉시 끊어 presence offline·in-flight 작업 re-queue를 유발한다. 실패는 흡수
        # (전이·revoke는 이미 성립 — WS 정리 실패가 owner 격리를 되돌리지 않는다).
        if self._disconnect_owner is not None and from_owner != to_owner:
            try:
                self._disconnect_owner(from_owner)
            except Exception:
                pass

        if self._journal_sink is not None:
            self._journal_sink.append_transfer(
                **_journal_kwargs(candidate, by=by, at=self._clock())
            )

        audit_index = self._record_action(
            action="OwnershipTransfer",
            subject_id=card.agent_id,
            by=by,
            detail={
                "from_owner": from_owner,
                "to_owner": to_owner,
                "revoked_token_ids": revoked_token_ids,
            },
        )
        return OwnershipTransferResult(
            agent_id=card.agent_id,
            from_owner=from_owner,
            to_owner=to_owner,
            revoked_token_ids=revoked_token_ids,
            audit_index=audit_index,
        )

    def _revoke_owner_tokens(self, owner_id: str) -> list[str]:
        """구 owner의 활성 토큰을 전부 revoke한다(append-only·멱등·ADR 0026 재사용).

        `list_active`로 활성 토큰을 훑어 그 owner 것만 revoke한다. 토큰 스토어가
        미주입이면 no-op(빈 목록) — 워커 admission을 안 쓰는 배치(하위호환).
        """
        if self._token_store is None:
            return []
        now = self._clock()
        revoked: list[str] = []
        active: list[AdmissionToken] = self._token_store.list_active(now=now)
        for token in active:
            if token.owner_id == owner_id:
                self._token_store.revoke(token.token_id, now=now)
                revoked.append(token.token_id)
        return revoked

    def _record_action(
        self, *, action: str, subject_id: str, by: str, detail: dict[str, Any]
    ) -> int:
        """감사 이벤트를 append하고 그 인덱스를 돌려준다(전이 ≠ 기록).

        미주입 sink면 -1(감사 없이 전이만 — 결정론 단위 테스트가 sink 없이 도메인을 본다).
        """
        if self._audit_sink is None:
            return -1
        from agent_org_network.audit import action_record

        record = action_record(
            timestamp=self._clock(),
            action=action,
            subject_id=subject_id,
            by=by,
            detail=detail,
        )
        self._audit_sink.record_action(record)
        # 인덱스는 records()가 있으면 마지막(append-only). AuditReader 미구현 sink면 -1.
        records_fn = getattr(self._audit_sink, "records", None)
        if callable(records_fn):
            result: object = records_fn()
            if isinstance(result, list):
                return len(cast("list[object]", result)) - 1
        return -1


def _journal_kwargs(candidate: CardCandidate, *, by: str, at: datetime) -> dict[str, Any]:
    """`CardCandidate`를 `RegistryJournalSink.append_*`의 kwargs 모양으로 편평화한다."""
    return {
        "agent_id": candidate.agent_id,
        "owner": candidate.owner,
        "team": candidate.team,
        "summary": candidate.summary,
        "domains": list(candidate.domains),
        "last_reviewed_at": candidate.last_reviewed_at,
        "by": by,
        "at": at,
        "maintainer": candidate.maintainer,
        "can_answer": list(candidate.can_answer),
        "cannot_answer": list(candidate.cannot_answer),
        "approval_when": list(candidate.approval_when),
        "collaborate_when": list(candidate.collaborate_when),
        "knowledge_sources": list(candidate.knowledge_sources),
        "trust_labels": list(candidate.trust_labels),
    }


def replay_registry_journal(journal: "SqliteRegistryJournal", registry: "Registry") -> None:
    """durable 저널을 처음부터 순서대로 재생해 라이브 Registry를 복원한다(ADR 0034 결정 1·2).

    중앙 기동 시 호출 순서: `registry.load(seed_dir)`(YAML 초기 시드) → `registry.
    validate()` → 이 함수(저널 리플레이). 저널의 각 항목을 `admit_card`(admission
    관문)에 그대로 태운다 — **우회 없음**("무효 카드는 등록되지 않는다" 불변식이
    리플레이에도 그대로 적용된다). 참조 무결성이 깨진 항목(owner/maintainer가
    당시엔 있었지만 지금 시드엔 없는 등)은 조용히 건너뛴다(복원 실패가 부팅을
    막지 않는다 — 안전측 스킵).

    `register` 항목은 `registry.register`(신규, 이미 있으면 스킵 — 멱등 방어),
    `transfer` 항목은 `registry.replace_card`(이미 있는 agent_id 값 교체)로
    반영한다. 순서(seq ASC)를 보존해 같은 agent_id에 여러 항목이 쌓여도(등록→
    오너변경→오너변경…) 마지막 상태가 최종 라이브 상태가 된다(전이 순서 재현).
    """
    from agent_org_network.registry import RegistryError

    for entry in journal.entries():
        candidate = CardCandidate(
            agent_id=entry.candidate.agent_id,
            owner=entry.candidate.owner,
            team=entry.candidate.team,
            summary=entry.candidate.summary,
            domains=list(entry.candidate.domains),
            last_reviewed_at=entry.candidate.last_reviewed_at,
            maintainer=entry.candidate.maintainer,
            can_answer=list(entry.candidate.can_answer),
            cannot_answer=list(entry.candidate.cannot_answer),
            approval_when=list(entry.candidate.approval_when),
            collaborate_when=list(entry.candidate.collaborate_when),
            knowledge_sources=list(entry.candidate.knowledge_sources),
            trust_labels=list(entry.candidate.trust_labels),
        )
        card, _errors = admit_card(candidate, registry)
        if card is None:
            continue  # 참조 무결성 붕괴 등 — 안전측 스킵(부팅 중단 없음).

        if entry.kind == "register":
            if registry.has_card(card.agent_id):
                continue  # 멱등 방어(이미 존재 — 중복 재생 무시).
            try:
                registry.register(card)
            except RegistryError:
                continue
        else:  # "transfer"
            if not registry.has_card(card.agent_id):
                continue  # 전이 대상 부재 — 안전측 스킵.
            registry.replace_card(card)


__all__ = [
    "CardCandidate",
    "admit_card",
    "AdmissionError",
    "DuplicateCardError",
    "UnknownCardError",
    "OwnershipTransferResult",
    "AdminAuditSink",
    "RegistryJournalSink",
    "AdminRegistryService",
    "replay_registry_journal",
]
