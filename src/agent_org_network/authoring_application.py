"""저작 HTTP/MCP가 함께 쓰는 중앙 권한·승인·감사 경계.

Raw 문서와 staged OKF는 이 application에 보관하지 않는다. 이 경계는 현재
``AgentCard``로 ``ResourceRef``를 다시 만들고, caller가 제공한 owner-side 작업을
허용된 시점에만 실행한다. Git commit·audit·central index의 원자성은 제공하지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypeVar

from agent_org_network.agent_card import AgentCard
from agent_org_network.audit import AuditLog, action_record
from agent_org_network.central_authority import AuthenticatedPrincipal, ResourceRef
from agent_org_network.operational_application import (
    OperationalDeniedError,
    MutationApprovalProvider,
    OperationalMutationApproval,
    OperationalNotFoundError,
    OperationalUnavailableError,
    mutation_approval_binding,
)
from agent_org_network.operational_authorization import OperationalAction, OperationalAuthorization


class AuthorRegistry(Protocol):
    def get(self, agent_id: str) -> AgentCard: ...


_T = TypeVar("_T")


# writer callback은 저작 원문·변경 참조·외부 시스템 응답을 가질 수 있다. 성공 audit에는
# 운영자가 이해할 수 있는 제한된 작업 종류만 투영한다. callback detail을 그대로 병합하면
# 그 비밀/원문이 append-only 감사 로그에 남는다.
_AUDITABLE_MUTATION_OPERATIONS = frozenset({"builder_commit", "publish", "edit", "delete"})


@dataclass(frozen=True)
class AuthoringMutation:
    """성공 audit에 필요한 식별자만 담는 persistent 저작 결과."""

    resource_id: str
    detail: dict[str, object]


class AuthoringApplication:
    """중앙 저작의 query/run/persistent mutation 공통 boundary.

    ``run``은 transient owner-side 처리라 승인·감사를 요구하지 않는다. ``mutate``는
    현재 사람 승인 증적과 audit sink가 있어야만 callback을 호출한다. callback은 raw
    문서나 staged 자료를 application 밖에서만 유지한다. 단, 임의 ``OkfFile`` 묶음처럼
    현재 domain으로 재admission·재렌더링할 수 없는 legacy Builder 입력에는 이 경계를
    쓰지 않는다. 해당 HTTP 경로는 구조화된 BuilderDraft migration 전 중앙 모드에서 닫힌다.
    """

    def __init__(
        self,
        *,
        authorization: OperationalAuthorization,
        registry: AuthorRegistry,
        mutation_approval: MutationApprovalProvider | None,
        audit_log: AuditLog | None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._authorization = authorization
        self._registry = registry
        self._mutation_approval = mutation_approval
        self._audit_log = audit_log
        self._clock = clock

    @staticmethod
    def _require(action: OperationalAction, expected: OperationalAction) -> None:
        if action != expected:
            raise OperationalDeniedError()

    def _card(self, agent_id: str) -> AgentCard:
        try:
            return self._registry.get(agent_id)
        except KeyError as error:
            raise OperationalNotFoundError() from error

    @staticmethod
    def _resource(principal: AuthenticatedPrincipal, card: AgentCard) -> ResourceRef:
        return ResourceRef(
            org_id=principal.org_id,
            kind="agent_card",
            resource_id=card.agent_id,
            owner_subject_id=card.owner,
        )

    def _authorize(
        self, principal: AuthenticatedPrincipal, action: OperationalAction, card: AgentCard
    ) -> None:
        outcome = self._authorization.authorize(principal, action, self._resource(principal, card))
        if outcome == "unavailable":
            raise OperationalUnavailableError()
        if outcome != "allowed":
            raise OperationalDeniedError()

    @staticmethod
    def _audit_mutation_detail(mutation: AuthoringMutation) -> dict[str, str]:
        """callback 결과에서 allowlist된 비밀 없는 감사 메타데이터만 고른다."""
        operation = mutation.detail.get("operation")
        if type(operation) is str and operation in _AUDITABLE_MUTATION_OPERATIONS:
            return {"operation": operation}
        # callback의 임의 detail은 성공 기록의 필수 계약이 아니다. operation이 알려지지
        # 않았더라도 raw body/change_ref/secret을 감사에 흘리지 않고 고정 필드만 남긴다.
        return {}

    def current_card(
        self, principal: AuthenticatedPrincipal, agent_id: str, *, action: OperationalAction
    ) -> AgentCard:
        """현재 카드/owner와 sealed grant를 함께 확인한다."""
        card = self._card(agent_id)
        self._authorize(principal, action, card)
        return card

    def query(
        self,
        principal: AuthenticatedPrincipal,
        agent_id: str,
        reader: Callable[[AgentCard], _T],
        *,
        action: OperationalAction = "author.read",
    ) -> _T:
        self._require(action, "author.read")
        card = self.current_card(principal, agent_id, action=action)
        result = reader(card)
        # DTO를 외부로 내기 바로 전에 current grant/card를 다시 확인한다.
        self.current_card(principal, agent_id, action=action)
        return result

    def run(
        self,
        principal: AuthenticatedPrincipal,
        agent_id: str,
        runner: Callable[[AgentCard], _T],
        *,
        re_admit: Callable[[_T, AgentCard], _T] | None = None,
        action: OperationalAction = "author.write",
    ) -> _T:
        self._require(action, "author.write")
        card = self.current_card(principal, agent_id, action=action)
        result = runner(card)
        # LLM 뒤에는 바뀐 domain으로 owner-side admission을 다시 해야 한다.
        card = self.current_card(principal, agent_id, action=action)
        if re_admit is not None:
            return re_admit(result, card)
        return result

    def mutate(
        self,
        principal: AuthenticatedPrincipal,
        agent_id: str,
        writer: Callable[[AgentCard], tuple[_T, AuthoringMutation]],
        *,
        channel: str,
        command: object,
        after_write: Callable[[_T, AgentCard], _T] | None = None,
        action: OperationalAction = "author.publish",
    ) -> _T:
        """승인 후 Git 직전 재인가·Git 뒤 index 전 재인가를 강제한다.

        ``writer``는 Git 직전 호출된다. writer 내부에서는 전달받은 최신 card로 admission
        을 다시 수행해야 하며, Git 이후 central index write 전 callback 결과가 바깥으로
        돌아오기 전 마지막 재인가가 수행된다.
        """
        self._require(action, "author.publish")
        card = self.current_card(principal, agent_id, action=action)
        audit = self._audit_log
        if audit is None:
            raise OperationalUnavailableError()
        approval_provider = self._mutation_approval
        if approval_provider is None:
            raise OperationalUnavailableError()
        try:
            resource = self._resource(principal, card)
            binding = mutation_approval_binding(
                action,
                resource,
                command,
                resource_revision=card.model_dump(mode="json"),
            )
            approval = approval_provider(
                principal, action, resource, binding.command_digest, binding.resource_fingerprint
            )
        except Exception as error:
            raise OperationalUnavailableError() from error
        if type(approval) is not OperationalMutationApproval:
            raise OperationalUnavailableError()
        if approval.outcome == "unavailable":
            raise OperationalUnavailableError()
        if (
            approval.outcome != "allowed"
            or not approval.evidence_id
            or not approval.evidence_id.strip()
            or approval.command_digest != binding.command_digest
            or approval.resource_fingerprint != binding.resource_fingerprint
        ):
            raise OperationalDeniedError()
        # Git prewrite: card/owner/정책을 다시 읽어 기존 grant 재사용을 금지한다.
        card = self.current_card(principal, agent_id, action=action)
        if (
            mutation_approval_binding(
                action,
                self._resource(principal, card),
                command,
                resource_revision=card.model_dump(mode="json"),
            ).resource_fingerprint
            != approval.resource_fingerprint
        ):
            raise OperationalDeniedError()
        result, mutation = writer(card)
        # Git은 이미 완료됐을 수 있다. 그래도 central index write/성공 DTO 이전에는 revoke를
        # 다시 확인한다. audit도 성공한 변경 뒤에만 남긴다.
        current_after_write = self.current_card(principal, agent_id, action=action)
        if after_write is not None:
            result = after_write(result, current_after_write)
        audit.record_action(
            action_record(
                timestamp=self._clock(),
                action=action,
                subject_id=mutation.resource_id,
                by=principal.subject_id,
                detail={
                    "channel": channel,
                    "outcome": "succeeded",
                    "approval_evidence_id": approval.evidence_id,
                    "approval_command_digest": approval.command_digest,
                    **self._audit_mutation_detail(mutation),
                },
            )
        )
        return result
