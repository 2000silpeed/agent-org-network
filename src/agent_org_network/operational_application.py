"""운영 HTTP/MCP가 함께 쓰는 권한-우선 application 포트.

adapter는 이 모듈에 actor/조직을 전달하지 않는다. 인증된 principal은 server-side
resolver에서만 오며, 이 application은 매 조회와 변경 직전에 authoritative object로
ResourceRef를 다시 만든다. 따라서 오래된 카드 owner나 session owner로 받은 grant를
변경에 재사용할 수 없다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import Any, Protocol

from agent_org_network.audit import AuditLog, AuditReader, action_record
from agent_org_network.agent_card import AgentCard
from agent_org_network.admin_registry import (
    AdminRegistryService,
    CardCandidate,
    OwnershipTransferResult,
)
from agent_org_network.central_authority import AuthenticatedPrincipal, ResourceRef
from agent_org_network.operational_authorization import OperationalAction, OperationalAuthorization
from agent_org_network.operational_source_scope import (
    OperationalSourceKind,
    OperationalSourceScopeProofs,
)
from agent_org_network.session import Session, SessionStore


class RegistryReader(Protocol):
    def get(self, agent_id: str) -> AgentCard: ...

    def all_cards(self) -> list[AgentCard]: ...


class OperationalUnavailableError(Exception):
    pass


class OperationalDeniedError(Exception):
    pass


class OperationalNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class OperationalMutationApproval:
    """사람 승인/HITL 증거 포트의 최소 성공 결과(비밀·원문은 담지 않는다)."""

    outcome: str
    evidence_id: str | None = None
    # 승인 시스템은 원문이나 자격 증명을 다시 돌려주지 않는다. 대신 이 두 값이 요청한
    # command와 승인 당시의 ResourceRef를 정확히 나타내야 한다.
    command_digest: str | None = None
    resource_fingerprint: str | None = None


@dataclass(frozen=True)
class MutationApprovalBinding:
    """승인 포트와 audit가 공유하는 비밀 없는 command 결박값."""

    command_digest: str
    resource_fingerprint: str


MutationApprovalProvider = Callable[
    [AuthenticatedPrincipal, OperationalAction, ResourceRef, str, str], OperationalMutationApproval
]


def _sha256_canonical(value: object) -> str:
    """JSON-safe 값의 결정론적 SHA-256.

    호출자는 비밀/원문을 넣을 수 있지만, 이 모듈은 그 값을 보관·감사하지 않고 해시만
    전달한다. JSON으로 정규화할 수 없는 값은 fail-closed 대상이다.
    """
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise OperationalUnavailableError() from error
    return sha256(encoded).hexdigest()


def mutation_approval_binding(
    action: OperationalAction,
    resource: ResourceRef,
    command: object,
    *,
    resource_revision: object | None = None,
) -> MutationApprovalBinding:
    """action·현재 리소스 정체성·명령 내용을 하나의 재사용 불가 승인 결박으로 만든다."""
    resource_value = {
        "org_id": resource.org_id,
        "kind": resource.kind,
        "resource_id": resource.resource_id,
        "owner_subject_id": resource.owner_subject_id,
    }
    # revision이 있는 리소스(카드·세션)는 identity만으로 충분하지 않다. 현재 snapshot의
    # 해시를 포함해 owner는 같아도 카드/세션 상태가 바뀐 승인 증거를 재사용하지 못하게 한다.
    resource_fingerprint = _sha256_canonical(
        {"identity": resource_value, "revision": resource_revision}
    )
    return MutationApprovalBinding(
        resource_fingerprint=resource_fingerprint,
        command_digest=_sha256_canonical(
            {
                "action": action,
                "resource_fingerprint": resource_fingerprint,
                "command": command,
            }
        ),
    )


class OperationalApplication:
    """P0 운영 query/command의 channel-independent application boundary."""

    def __init__(
        self,
        *,
        authorization: OperationalAuthorization,
        registry: RegistryReader,
        audit_reader: AuditReader | None,
        audit_log: AuditLog | None,
        session_store: SessionStore,
        hitl_is_on: Callable[[str], bool],
        hitl_set: Callable[[str, bool], None],
        hitl_is_explicit: Callable[[str], bool],
        hitl_mark_explicit: Callable[[str], None],
        hitl_seed: Callable[[Any], bool],
        monitor_summary: Callable[[int, dict[str, Any]], dict[str, Any]],
        org_graph: Callable[[], dict[str, Any]],
        mutation_approval: MutationApprovalProvider,
        admin_registry_service: AdminRegistryService | None = None,
        source_scope_proofs: OperationalSourceScopeProofs | None = None,
        audit_source: object | None = None,
        hitl_source: object | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._authorization = authorization
        self._registry = registry
        self._audit_reader = audit_reader
        self._audit_log = audit_log
        self._sessions = session_store
        self._hitl_is_on = hitl_is_on
        self._hitl_set = hitl_set
        self._hitl_is_explicit = hitl_is_explicit
        self._hitl_mark_explicit = hitl_mark_explicit
        self._hitl_seed = hitl_seed
        self._monitor_summary = monitor_summary
        self._org_graph = org_graph
        self._mutation_approval = mutation_approval
        self._admin_registry_service = admin_registry_service
        self._source_scope_proofs = source_scope_proofs
        self._audit_source = (
            audit_source if audit_source is not None else (audit_reader, audit_log)
        )
        self._hitl_source = hitl_source
        self._clock = clock

    def _authorize(
        self, principal: AuthenticatedPrincipal, action: OperationalAction, resource: ResourceRef
    ) -> None:
        outcome = self._authorization.authorize(principal, action, resource)
        if outcome == "unavailable":
            raise OperationalUnavailableError()
        if outcome != "allowed":
            raise OperationalDeniedError()

    def _require_source_scope(self, *kinds: OperationalSourceKind) -> None:
        proofs = self._source_scope_proofs
        sources: dict[OperationalSourceKind, object] = {
            "registry": self._registry,
            "graph": self._org_graph,
            "session": self._sessions,
            "audit": self._audit_source,
            "hitl": self._hitl_source,
        }
        if type(proofs) is not OperationalSourceScopeProofs or not proofs.verify(
            **{kind: sources[kind] for kind in kinds}
        ):
            raise OperationalUnavailableError()

    @staticmethod
    def _require_action(action: OperationalAction, expected: OperationalAction) -> None:
        """채널이 선언한 action이 이 application operation과 정확히 같은지 확인한다."""
        if action != expected:
            raise OperationalDeniedError()

    @staticmethod
    def _org_resource(principal: AuthenticatedPrincipal) -> ResourceRef:
        return ResourceRef(
            org_id=principal.org_id, kind="organization", resource_id=principal.org_id
        )

    def _card(self, agent_id: str) -> AgentCard:
        try:
            return self._registry.get(agent_id)
        except KeyError as error:
            raise OperationalNotFoundError() from error

    def _card_resource(self, principal: AuthenticatedPrincipal, card: AgentCard) -> ResourceRef:
        return ResourceRef(
            org_id=principal.org_id,
            kind="agent_card",
            resource_id=card.agent_id,
            owner_subject_id=card.owner,
        )

    @staticmethod
    def _card_revision(card: AgentCard) -> str:
        return _sha256_canonical(card.model_dump(mode="json"))

    def bind_admin_registry_service(self, service: AdminRegistryService) -> None:
        """같은 Registry를 쓰는 관리 도메인 서비스를 명시적으로 연결한다.

        web composition은 Registry journal replay 뒤에야 service를 만들므로 생성자 주입만
        강제하지 않는다. 이 binding은 기동 조립 단계에서 한 번만 허용한다.
        """
        if type(service) is not AdminRegistryService or self._admin_registry_service is not None:
            raise OperationalUnavailableError()
        self._admin_registry_service = service

    def _admin_registry(self) -> AdminRegistryService:
        service = self._admin_registry_service
        if type(service) is not AdminRegistryService:
            raise OperationalUnavailableError()
        return service

    @staticmethod
    def _candidate_from_card(card: AgentCard, *, owner: str) -> CardCandidate:
        return CardCandidate(
            agent_id=card.agent_id,
            owner=owner,
            team=card.team,
            summary=card.summary,
            domains=list(card.domains),
            last_reviewed_at=card.last_reviewed_at.isoformat(),
            maintainer=card.maintainer,
            can_answer=list(card.can_answer),
            cannot_answer=list(card.cannot_answer),
            approval_when=list(card.approval_when),
            collaborate_when=list(card.collaborate_when),
            knowledge_sources=list(card.knowledge_sources),
            trust_labels=list(card.trust_labels),
        )

    def list_cards(
        self, principal: AuthenticatedPrincipal, *, action: OperationalAction = "card.read"
    ) -> list[AgentCard]:
        """현재 카드 각각을 DTO 직전에 재인가해 운영 목록을 만든다."""
        self._require_action(action, "card.read")
        self._require_source_scope("registry")
        try:
            cards = list(self._registry.all_cards())
        except Exception as error:
            raise OperationalUnavailableError() from error
        result: list[AgentCard] = []
        for snapshot in sorted(cards, key=lambda card: card.agent_id):
            card = self._card(snapshot.agent_id)
            self._authorize(principal, action, self._card_resource(principal, card))
            card = self._card(snapshot.agent_id)
            self._authorize(principal, action, self._card_resource(principal, card))
            self._require_source_scope("registry")
            result.append(card)
        return result

    def card(
        self,
        principal: AuthenticatedPrincipal,
        agent_id: str,
        *,
        action: OperationalAction = "card.read",
    ) -> AgentCard:
        self._require_action(action, "card.read")
        self._require_source_scope("registry")
        card = self._card(agent_id)
        self._authorize(principal, action, self._card_resource(principal, card))
        card = self._card(agent_id)
        self._authorize(principal, action, self._card_resource(principal, card))
        self._require_source_scope("registry")
        return card

    def register_card(
        self,
        principal: AuthenticatedPrincipal,
        candidate: CardCandidate,
        *,
        channel: str,
        action: OperationalAction = "card.register",
    ) -> AgentCard:
        """후보 owner를 ResourceRef에 묶어 승인·감사 후에만 admission을 호출한다."""
        self._require_action(action, "card.register")
        self._require_source_scope("registry", "audit")
        resource = ResourceRef(
            org_id=principal.org_id,
            kind="agent_card",
            resource_id=candidate.agent_id,
            owner_subject_id=candidate.owner,
        )
        self._authorize(principal, action, resource)
        self._require_audit_sink()
        approval = self._require_mutation_approval(
            principal,
            action,
            resource,
            {"candidate": asdict(candidate)},
            resource_revision=_sha256_canonical(asdict(candidate)),
        )
        # 등록 대상은 approval 대기 중에도 다른 관리자가 만들 수 있다. 이 경우 후보 owner로
        # 만든 이전 ResourceRef를 현재 카드에 재사용하면 안 된다. 실제 admission 직전 다시
        # 조회해 아직 비어 있는 대상만 후보 ResourceRef로 재인가한다.
        try:
            self._registry.get(candidate.agent_id)
        except KeyError:
            pass
        else:
            raise OperationalDeniedError()
        self._require_source_scope("registry", "audit")
        self._authorize(principal, action, resource)
        card = self._admin_registry().register_card(candidate, by=principal.subject_id)
        self._record_success(
            action,
            card.agent_id,
            principal.subject_id,
            channel,
            {"owner": card.owner, **self._approval_audit_detail(approval)},
        )
        return card

    def transfer_card_owner(
        self,
        principal: AuthenticatedPrincipal,
        agent_id: str,
        new_owner: str,
        *,
        channel: str,
        action: OperationalAction = "card.transfer_owner",
    ) -> OwnershipTransferResult:
        """현재 카드와 바뀔 후보 양쪽을 write 직전에 재인가한다."""
        self._require_action(action, "card.transfer_owner")
        self._require_source_scope("registry", "audit")
        current = self._card(agent_id)
        self._require_source_scope("registry", "audit")
        self._authorize(principal, action, self._card_resource(principal, current))
        self._require_audit_sink()
        resource = self._card_resource(principal, current)
        approval = self._require_mutation_approval(
            principal,
            action,
            resource,
            {"agent_id": agent_id, "new_owner": new_owner},
            resource_revision=self._card_revision(current),
        )
        current = self._card(agent_id)
        current_resource = self._card_resource(principal, current)
        candidate = self._candidate_from_card(current, owner=new_owner)
        candidate_resource = ResourceRef(
            org_id=principal.org_id,
            kind="agent_card",
            resource_id=candidate.agent_id,
            owner_subject_id=candidate.owner,
        )
        self._authorize(principal, action, current_resource)
        self._authorize(principal, action, candidate_resource)
        if (
            mutation_approval_binding(
                action,
                current_resource,
                {"agent_id": agent_id, "new_owner": new_owner},
                resource_revision=self._card_revision(current),
            ).resource_fingerprint
            != approval.resource_fingerprint
        ):
            raise OperationalDeniedError()
        self._require_source_scope("registry", "audit")
        result = self._admin_registry().transfer_ownership(candidate, by=principal.subject_id)
        self._record_success(
            action,
            result.agent_id,
            principal.subject_id,
            channel,
            {
                "from_owner": result.from_owner,
                "to_owner": result.to_owner,
                **self._approval_audit_detail(approval),
            },
        )
        return result

    @staticmethod
    def _session_resource(principal: AuthenticatedPrincipal, session: Session) -> ResourceRef:
        return ResourceRef(
            org_id=principal.org_id,
            kind="session",
            resource_id=session.session_id,
            owner_subject_id=session.user_id,
        )

    @staticmethod
    def _session_revision(session: Session) -> dict[str, str]:
        # transcript 원문은 approval 경계까지 보낼 필요가 없다. 종료에 영향을 주는 현재
        # 상태·시각만 snapshot identity로 쓴다.
        return {
            "status": session.status,
            "started_at": session.started_at.isoformat(),
            "last_active_at": session.last_active_at.isoformat(),
        }

    def monitor(
        self, principal: AuthenticatedPrincipal, *, action: OperationalAction = "monitor.read"
    ) -> list[dict[str, Any]]:
        self._require_action(action, "monitor.read")
        self._require_source_scope("audit")
        resource = self._org_resource(principal)
        self._authorize(principal, action, resource)
        reader = self._audit_reader
        records = [] if reader is None else reader.records()
        self._require_source_scope("audit")
        self._authorize(principal, action, self._org_resource(principal))
        return [self._monitor_summary(index, record) for index, record in enumerate(records)]

    def audit_detail(
        self,
        principal: AuthenticatedPrincipal,
        index: int,
        *,
        action: OperationalAction = "audit.read",
    ) -> dict[str, Any]:
        self._require_action(action, "audit.read")
        self._require_source_scope("audit")
        self._authorize(principal, action, self._org_resource(principal))
        reader = self._audit_reader
        record = None if reader is None else reader.record_at(index)
        if record is None:
            raise OperationalNotFoundError()
        self._require_source_scope("audit")
        self._authorize(principal, action, self._org_resource(principal))
        return record

    def graph(
        self, principal: AuthenticatedPrincipal, *, action: OperationalAction = "org_graph.read"
    ) -> dict[str, Any]:
        self._require_action(action, "org_graph.read")
        self._require_source_scope("registry", "graph")
        self._authorize(principal, action, self._org_resource(principal))
        graph = self._org_graph()
        self._require_source_scope("registry", "graph")
        self._authorize(principal, action, self._org_resource(principal))
        return graph

    def session(
        self,
        principal: AuthenticatedPrincipal,
        session_id: str,
        *,
        action: OperationalAction = "session.end",
    ) -> Session:
        self._require_action(action, "session.end")
        self._require_source_scope("session")
        session = self._sessions.get(session_id)
        if session is None:
            raise OperationalNotFoundError()
        self._authorize(principal, action, self._session_resource(principal, session))
        self._require_source_scope("session")
        session = self._sessions.get(session_id)
        if session is None:
            raise OperationalNotFoundError()
        self._authorize(principal, action, self._session_resource(principal, session))
        return session

    def end_session(
        self,
        principal: AuthenticatedPrincipal,
        session_id: str,
        *,
        channel: str,
        action: OperationalAction = "session.end",
    ) -> Session:
        self._require_action(action, "session.end")
        self._require_source_scope("session", "audit")
        self.session(principal, session_id, action=action)
        # 감사 절차 기록은 중앙 운영 변경의 필수 사전조건이다. sink가 없는 조립은
        # end()를 호출하기 전에 unavailable로 끝낸다. record_action 실패와 변경의
        # 원자성은 이 in-process boundary가 주장하지 않는 별도 durable-UoW 범위다.
        self._require_audit_sink()
        # write 직전의 authoritative 재조회/재인가. end()는 한 번만 호출한다.
        current = self._sessions.get(session_id)
        if current is None:
            raise OperationalNotFoundError()
        resource = self._session_resource(principal, current)
        self._require_source_scope("session", "audit")
        self._authorize(principal, action, resource)
        approval = self._require_mutation_approval(
            principal,
            action,
            resource,
            {"session_id": session_id, "operation": "end"},
            resource_revision=self._session_revision(current),
        )
        # Evidence is tied to the resource identity observed above. A changed session owner
        # cannot reuse it even if an independent reauthorization would allow the new owner.
        current_after_approval = self._sessions.get(session_id)
        if (
            current_after_approval is None
            or mutation_approval_binding(
                action,
                self._session_resource(principal, current_after_approval),
                {"session_id": session_id, "operation": "end"},
                resource_revision=self._session_revision(current_after_approval),
            ).resource_fingerprint
            != approval.resource_fingerprint
        ):
            raise OperationalDeniedError()
        # 승인 응답 뒤에도 policy/role은 철회될 수 있다. 현재 session ResourceRef로 정확히
        # 한 번 더 확인한 바로 다음에만 변경 store를 호출한다.
        self._authorize(
            principal,
            action,
            self._session_resource(principal, current_after_approval),
        )
        self._require_source_scope("session", "audit")
        ended = self._sessions.end(session_id)
        if ended is None:
            raise OperationalNotFoundError()
        self._record_success(
            action,
            ended.session_id,
            principal.subject_id,
            channel,
            self._approval_audit_detail(approval),
        )
        return ended

    def hitl(
        self,
        principal: AuthenticatedPrincipal,
        agent_id: str,
        *,
        action: OperationalAction = "hitl.read",
    ) -> bool:
        self._require_action(action, "hitl.read")
        self._require_source_scope("registry", "hitl")
        card = self._card(agent_id)
        self._authorize(principal, action, self._card_resource(principal, card))
        card = self._card(agent_id)
        value = (
            self._hitl_is_on(agent_id)
            if self._hitl_is_explicit(agent_id)
            else self._hitl_seed(card)
        )
        self._require_source_scope("registry", "hitl")
        self._authorize(principal, action, self._card_resource(principal, card))
        return value

    def set_hitl(
        self,
        principal: AuthenticatedPrincipal,
        agent_id: str,
        on: bool,
        *,
        channel: str,
        action: OperationalAction = "hitl.write",
    ) -> bool:
        self._require_action(action, "hitl.write")
        self._require_source_scope("registry", "hitl", "audit")
        card = self._card(agent_id)
        self._require_source_scope("registry", "hitl", "audit")
        resource = self._card_resource(principal, card)
        self._authorize(principal, action, resource)
        self._require_audit_sink()
        approval = self._require_mutation_approval(
            principal,
            action,
            resource,
            {"agent_id": agent_id, "on": on},
            resource_revision=self._card_revision(card),
        )
        # write 직전 card/owner를 다시 본다.
        card = self._card(agent_id)
        current_resource = self._card_resource(principal, card)
        self._authorize(principal, action, current_resource)
        if (
            mutation_approval_binding(
                action,
                current_resource,
                {"agent_id": agent_id, "on": on},
                resource_revision=self._card_revision(card),
            ).resource_fingerprint
            != approval.resource_fingerprint
        ):
            raise OperationalDeniedError()
        self._require_source_scope("registry", "hitl", "audit")
        self._hitl_set(agent_id, on)
        self._hitl_mark_explicit(agent_id)
        self._record_success(
            action,
            agent_id,
            principal.subject_id,
            channel,
            {"on": on, **self._approval_audit_detail(approval)},
        )
        return on

    def _require_mutation_approval(
        self,
        principal: AuthenticatedPrincipal,
        action: OperationalAction,
        resource: ResourceRef,
        command: object,
        *,
        resource_revision: object | None = None,
    ) -> OperationalMutationApproval:
        binding = mutation_approval_binding(
            action, resource, command, resource_revision=resource_revision
        )
        try:
            decision = self._mutation_approval(
                principal, action, resource, binding.command_digest, binding.resource_fingerprint
            )
        except Exception as error:
            raise OperationalUnavailableError() from error
        if type(decision) is not OperationalMutationApproval:
            raise OperationalUnavailableError()
        if decision.outcome == "unavailable":
            raise OperationalUnavailableError()
        if (
            decision.outcome != "allowed"
            or not isinstance(decision.evidence_id, str)
            or not decision.evidence_id.strip()
            or decision.command_digest != binding.command_digest
            or decision.resource_fingerprint != binding.resource_fingerprint
        ):
            raise OperationalDeniedError()
        return decision

    @staticmethod
    def _approval_audit_detail(approval: OperationalMutationApproval) -> dict[str, str]:
        assert approval.evidence_id is not None and approval.command_digest is not None
        return {
            "approval_evidence_id": approval.evidence_id,
            "approval_command_digest": approval.command_digest,
        }

    def _record_success(
        self,
        action: str,
        subject_id: str,
        by: str,
        channel: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        audit_log = self._require_audit_sink()
        merged = {"channel": channel, "outcome": "succeeded", **(detail or {})}
        audit_log.record_action(
            action_record(
                timestamp=self._clock(), action=action, subject_id=subject_id, by=by, detail=merged
            )
        )

    def _require_audit_sink(self) -> AuditLog:
        """중앙 mutation에 필요한 append-only audit sink를 확정한다."""
        if self._audit_log is None:
            raise OperationalUnavailableError()
        return self._audit_log
