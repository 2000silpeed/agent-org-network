"""S3.2 read-only tenant operational composition; legacy operational paths are excluded."""
# pyright: reportArgumentType=false, reportReturnType=false

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3

from agent_org_network.central_authority import AuthenticatedPrincipal, ResourceRef
from agent_org_network.operational_authorization import OperationalAction, OperationalAuthorization
from agent_org_network.sqlite_tenant_port_audit_adapter import (
    SqliteTenantAuditReader,
    SqliteTenantAuditWriter,
)
from agent_org_network.sqlite_tenant_state_adapters import (
    SqliteTenantGraphAdapter,
    SqliteTenantHitlAdapter,
    SqliteTenantRegistryAdapter,
    SqliteTenantSessionAdapter,
)
from agent_org_network.tenant_operational_ports import (
    SafeAuditEvent,
    ResourceFingerprint,
    ScopedUnavailable,
    TenantCard,
    TenantOrgId,
    TenantSession,
)
from agent_org_network.tenant_operational_approval import (
    TenantOperationalApprovalEvidence,
    TenantOperationalApprovalPort,
    canonical_tenant_operational_command_digest,
    canonical_tenant_operational_resource_fingerprint,
)
from agent_org_network.sqlite_durable_tenant_operational_mutations import (
    capture_sqlite_tenant_operational_mutation_scope_snapshot,
)
from agent_org_network.sqlite_durable_tenant_operational_authorization import (
    open_sqlite_durable_tenant_operational_authorization,
)
from agent_org_network.sqlite_tenant_operational_mutation_uow import (
    CardRegisterCommand,
    CardTransferOwnerCommand,
    HitlWriteCommand,
    SessionEndCommand,
    TenantOperationalAuthorizationBinding,
    TenantOperationalMutationCommand,
    TenantOperationalMutationReceipt,
    execute_sqlite_tenant_operational_mutation,
)


class TenantOperationalUnavailable(Exception):
    pass


class TenantOperationalMutationUnavailable(TenantOperationalUnavailable):
    code = "operational_mutation_uow_unavailable"


@dataclass(frozen=True)
class TenantOperationalMutationPlan:
    command: TenantOperationalMutationCommand
    action: OperationalAction
    pre_resource: ResourceRef
    post_resource: ResourceRef
    pre_fingerprint: ResourceFingerprint
    command_digest: str


@dataclass(frozen=True)
class _PostMirror:
    resource: ResourceRef
    fingerprint: ResourceFingerprint


@dataclass(frozen=True)
class SqliteTenantOperationalDependencies:
    org: TenantOrgId
    registry: SqliteTenantRegistryAdapter
    graph: SqliteTenantGraphAdapter
    session: SqliteTenantSessionAdapter
    audit_reader: SqliteTenantAuditReader
    audit_writer: SqliteTenantAuditWriter
    hitl: SqliteTenantHitlAdapter


def sqlite_tenant_operational_dependencies(
    connection: sqlite3.Connection, org: TenantOrgId
) -> SqliteTenantOperationalDependencies:
    if type(org) is not TenantOrgId:
        raise ValueError("exact TenantOrgId가 필요합니다.")
    return SqliteTenantOperationalDependencies(
        org,
        SqliteTenantRegistryAdapter(connection, org),
        SqliteTenantGraphAdapter(connection, org),
        SqliteTenantSessionAdapter(connection, org),
        SqliteTenantAuditReader(connection, org),
        SqliteTenantAuditWriter(connection, org),
        SqliteTenantHitlAdapter(connection, org),
    )


class TenantOperationalApplication:
    def __init__(
        self,
        *,
        dependencies: SqliteTenantOperationalDependencies,
        authorization: OperationalAuthorization,
    ) -> None:
        if (
            type(dependencies) is not SqliteTenantOperationalDependencies
            or type(authorization) is not OperationalAuthorization
        ):
            raise ValueError("exact tenant dependencies와 authorization이 필요합니다.")
        self._d, self._authorization = dependencies, authorization

    def _authorize(
        self, principal: AuthenticatedPrincipal, action: OperationalAction, resource: ResourceRef
    ) -> None:
        if (
            type(principal) is not AuthenticatedPrincipal
            or principal.org_id != self._d.org.value
            or self._authorization.authorize(principal, action, resource) != "allowed"
        ):
            raise TenantOperationalUnavailable()

    def _card_resource(self, card: TenantCard) -> ResourceRef:
        return ResourceRef(
            org_id=self._d.org.value,
            kind="agent_card",
            resource_id=card.card_id,
            owner_subject_id=card.owner_id,
        )

    def _sealed_post_fingerprint(self, resource: ResourceRef, state: object) -> ResourceFingerprint:
        # Post evidence must survive its own audit write, but no unrelated
        # source change may be invisible.  The state therefore contains the
        # target's revision-bearing DTO fingerprint and is itself sealed.
        source_digest = hashlib.sha256(
            json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return canonical_tenant_operational_resource_fingerprint(
            resource=resource, state=state, source_digest=source_digest
        )

    def _current_post_mirror(self, command: TenantOperationalMutationCommand) -> _PostMirror:
        if isinstance(command, CardRegisterCommand | CardTransferOwnerCommand):
            card = self._d.registry.card(self._d.org, command.card_id)
            if isinstance(card, ScopedUnavailable) or card.owner_id != command.owner_id:
                raise TenantOperationalUnavailable()
            resource = self._card_resource(card)
            state: object = {"card": card.fingerprint.value, "owner": card.owner_id}
        elif isinstance(command, SessionEndCommand):
            session = self._d.session.session(self._d.org, command.session_id)
            if isinstance(session, ScopedUnavailable) or session.status != "ended":
                raise TenantOperationalUnavailable()
            resource = ResourceRef(
                org_id=command.org_id,
                kind="session",
                resource_id=session.session_id,
                owner_subject_id=session.user_id,
            )
            state = {"session": session.fingerprint.value, "status": session.status}
        else:
            assert type(command) is HitlWriteCommand
            card = self._d.registry.card(self._d.org, command.card_id)
            enabled = self._d.hitl.read(self._d.org, command.card_id)
            if (
                isinstance(card, ScopedUnavailable)
                or isinstance(enabled, ScopedUnavailable)
                or enabled is not command.on
            ):
                raise TenantOperationalUnavailable()
            resource = self._card_resource(card)
            state = {"card": card.fingerprint.value, "on": enabled}
        return _PostMirror(resource, self._sealed_post_fingerprint(resource, state))

    def _planned_post_mirror(self, command: TenantOperationalMutationCommand) -> _PostMirror:
        """Compute the one CAS successor that the sealed command may create."""
        connection = self._d.registry._connection  # pyright: ignore[reportPrivateUsage]
        if isinstance(command, CardRegisterCommand | CardTransferOwnerCommand):
            row = connection.execute(
                "SELECT revision FROM operational_registry_state WHERE org_id=?", (command.org_id,)
            ).fetchone()
            if row is None or type(row[0]) is not int:
                raise TenantOperationalUnavailable()
            resource = ResourceRef(
                org_id=command.org_id,
                kind="agent_card",
                resource_id=command.card_id,
                owner_subject_id=command.owner_id,
            )
            card = ResourceFingerprint.from_scalars(
                "tenant-card-v1", command.org_id, str(row[0] + 1), command.card_id, command.owner_id
            )
            state: object = {"card": card.value, "owner": command.owner_id}
        elif isinstance(command, SessionEndCommand):
            row = connection.execute(
                "SELECT user_id,revision FROM operational_sessions WHERE org_id=? AND session_id=?",
                (command.org_id, command.session_id),
            ).fetchone()
            if row is None or type(row[0]) is not str or type(row[1]) is not int:
                raise TenantOperationalUnavailable()
            resource = ResourceRef(
                org_id=command.org_id,
                kind="session",
                resource_id=command.session_id,
                owner_subject_id=row[0],
            )
            session = ResourceFingerprint.from_scalars(
                "tenant-session-v1",
                command.org_id,
                command.session_id,
                row[0],
                "ended",
                str(row[1] + 1),
            )
            state = {"session": session.value, "status": "ended"}
        else:
            assert type(command) is HitlWriteCommand
            card = self._d.registry.card(self._d.org, command.card_id)
            row = connection.execute(
                "SELECT revision FROM operational_hitl_toggles WHERE org_id=? AND agent_id=?",
                (command.org_id, command.card_id),
            ).fetchone()
            if isinstance(card, ScopedUnavailable) or (row is not None and type(row[0]) is not int):
                raise TenantOperationalUnavailable()
            resource = self._card_resource(card)
            # HITL's row revision is represented by the target value plus the
            # revision-bearing Card snapshot; a new row starts at zero.
            state = {"card": card.fingerprint.value, "on": command.on}
        return _PostMirror(resource, self._sealed_post_fingerprint(resource, state))

    def cards(self, principal: AuthenticatedPrincipal) -> tuple[TenantCard, ...]:
        value = self._d.graph.derive(self._d.org)
        if isinstance(value, ScopedUnavailable):
            raise TenantOperationalUnavailable()
        result: list[TenantCard] = []
        for snapshot in value:
            self._authorize(principal, "card.read", self._card_resource(snapshot))
            current = self._d.registry.card(self._d.org, snapshot.card_id)
            if current != snapshot:
                raise TenantOperationalUnavailable()
            self._authorize(principal, "card.read", self._card_resource(current))
            result.append(current)
        return tuple(result)

    def card(self, principal: AuthenticatedPrincipal, card_id: str) -> TenantCard:
        value = self._d.registry.card(self._d.org, card_id)
        if isinstance(value, ScopedUnavailable):
            raise TenantOperationalUnavailable()
        self._authorize(principal, "card.read", self._card_resource(value))
        current = self._d.registry.card(self._d.org, card_id)
        if current != value:
            raise TenantOperationalUnavailable()
        self._authorize(principal, "card.read", self._card_resource(current))
        return current

    def session(self, principal: AuthenticatedPrincipal, session_id: str) -> TenantSession:
        value = self._d.session.session(self._d.org, session_id)
        if isinstance(value, ScopedUnavailable):
            raise TenantOperationalUnavailable()
        resource = ResourceRef(
            org_id=self._d.org.value,
            kind="session",
            resource_id=value.session_id,
            owner_subject_id=value.user_id,
        )
        self._authorize(principal, "session.end", resource)
        current = self._d.session.session(self._d.org, session_id)
        if current != value:
            raise TenantOperationalUnavailable()
        self._authorize(principal, "session.end", resource)
        return current

    def hitl(self, principal: AuthenticatedPrincipal, card_id: str) -> bool:
        card = self.card(principal, card_id)
        value = self._d.hitl.read(self._d.org, card_id)
        if isinstance(value, ScopedUnavailable):
            raise TenantOperationalUnavailable()
        self._authorize(principal, "hitl.read", self._card_resource(card))
        current = self._d.hitl.read(self._d.org, card_id)
        if current != value:
            raise TenantOperationalUnavailable()
        # HITL source가 안정적이어도 그 사이 카드 owner/revision이 바뀌면 이전
        # ResourceRef grant를 DTO 반환에 재사용할 수 없다.
        current_card = self._d.registry.card(self._d.org, card_id)
        if isinstance(current_card, ScopedUnavailable) or current_card != card:
            raise TenantOperationalUnavailable()
        self._authorize(principal, "hitl.read", self._card_resource(current_card))
        return current

    def audit(self, principal: AuthenticatedPrincipal) -> tuple[SafeAuditEvent, ...]:
        resource = ResourceRef(
            org_id=self._d.org.value, kind="organization", resource_id=self._d.org.value
        )
        value = self._d.audit_reader.list(self._d.org)
        if isinstance(value, ScopedUnavailable):
            raise TenantOperationalUnavailable()
        self._authorize(principal, "audit.read", resource)
        current = self._d.audit_reader.list(self._d.org)
        if current != value:
            raise TenantOperationalUnavailable()
        self._authorize(principal, "audit.read", resource)
        return current

    def graph(self, principal: AuthenticatedPrincipal) -> tuple[TenantCard, ...]:
        resource = ResourceRef(
            org_id=self._d.org.value, kind="organization", resource_id=self._d.org.value
        )
        value = self._d.graph.derive(self._d.org)
        if isinstance(value, ScopedUnavailable):
            raise TenantOperationalUnavailable()
        self._authorize(principal, "org_graph.read", resource)
        current = self._d.graph.derive(self._d.org)
        if current != value:
            raise TenantOperationalUnavailable()
        self._authorize(principal, "org_graph.read", resource)
        return current

    def audit_detail(self, principal: AuthenticatedPrincipal, sequence: int) -> SafeAuditEvent:
        resource = ResourceRef(
            org_id=self._d.org.value, kind="organization", resource_id=self._d.org.value
        )
        value = self._d.audit_reader.detail(self._d.org, sequence)
        if isinstance(value, ScopedUnavailable):
            raise TenantOperationalUnavailable()
        self._authorize(principal, "audit.read", resource)
        current = self._d.audit_reader.detail(self._d.org, sequence)
        if current != value:
            raise TenantOperationalUnavailable()
        self._authorize(principal, "audit.read", resource)
        return current

    def mutation_unavailable(self) -> None:
        """R1 durable UoW 전에는 어떤 tenant mutation port도 호출하지 않는다."""
        raise TenantOperationalMutationUnavailable()

    def plan_mutation(
        self, principal: AuthenticatedPrincipal, command: TenantOperationalMutationCommand
    ) -> TenantOperationalMutationPlan:
        """Build an approval-bound plan from a live pre-state, without writing."""
        if (
            principal.org_id != self._d.org.value
            or command.org_id != self._d.org.value
            or command.principal_id != principal.subject_id
        ):
            raise TenantOperationalUnavailable()
        connection = self._d.registry._connection  # pyright: ignore[reportPrivateUsage]
        scope = capture_sqlite_tenant_operational_mutation_scope_snapshot(connection)
        if command.expected_scope != scope:
            raise TenantOperationalUnavailable()
        if type(command) is CardRegisterCommand:
            action: OperationalAction = "card.register"
            cards = self._d.graph.derive(self._d.org)
            if isinstance(cards, ScopedUnavailable) or any(
                card.card_id == command.card_id for card in cards
            ):
                raise TenantOperationalUnavailable()
            pre = ResourceRef(
                org_id=command.org_id,
                kind="agent_card",
                resource_id=command.card_id,
                owner_subject_id=command.owner_id,
            )
            post = pre
            state: object = {"revision": scope.source_revision, "absence": True}
            effect: object = {"card_id": command.card_id, "owner_id": command.owner_id}
        elif type(command) is CardTransferOwnerCommand:
            action = "card.transfer_owner"
            prior = self._d.registry.card(self._d.org, command.card_id)
            if isinstance(prior, ScopedUnavailable):
                raise TenantOperationalUnavailable()
            pre = self._card_resource(prior)
            post = ResourceRef(
                org_id=command.org_id,
                kind="agent_card",
                resource_id=command.card_id,
                owner_subject_id=command.owner_id,
            )
            state = {"revision": scope.source_revision, "absence": False}
            effect = {"card_id": command.card_id, "owner_id": command.owner_id}
        elif type(command) is SessionEndCommand:
            action = "session.end"
            prior = self._d.session.session(self._d.org, command.session_id)
            if isinstance(prior, ScopedUnavailable) or prior.status != "active":
                raise TenantOperationalUnavailable()
            pre = ResourceRef(
                org_id=command.org_id,
                kind="session",
                resource_id=prior.session_id,
                owner_subject_id=prior.user_id,
            )
            post = pre
            state = {"revision": scope.source_revision, "absence": False}
            effect = {"session_id": command.session_id, "status": "ended"}
        elif type(command) is HitlWriteCommand:
            action = "hitl.write"
            prior = self._d.registry.card(self._d.org, command.card_id)
            if isinstance(prior, ScopedUnavailable):
                raise TenantOperationalUnavailable()
            pre = post = self._card_resource(prior)
            enabled = self._d.hitl.read(self._d.org, command.card_id)
            if isinstance(enabled, ScopedUnavailable):
                raise TenantOperationalUnavailable()
            state = {
                "revision": scope.source_revision,
                "absence": enabled is False,
            }
            effect = {"card_id": command.card_id, "on": command.on}
        else:
            raise TenantOperationalUnavailable()
        fingerprint = canonical_tenant_operational_resource_fingerprint(
            resource=pre, state=state, source_digest=scope.snapshot_digest
        )
        digest = canonical_tenant_operational_command_digest(
            org_id=command.org_id,
            principal_id=command.principal_id,
            action=action,
            resource_fingerprint=fingerprint,
            effect=effect,
        )
        self._authorize(principal, action, pre)
        return TenantOperationalMutationPlan(command, action, pre, post, fingerprint, digest)

    def approve_and_commit_mutation(
        self,
        principal: AuthenticatedPrincipal,
        plan: TenantOperationalMutationPlan,
        approval: TenantOperationalApprovalPort,
    ) -> TenantOperationalMutationReceipt:
        if (
            type(plan) is not TenantOperationalMutationPlan
            or principal.subject_id != plan.command.principal_id
        ):
            raise TenantOperationalUnavailable()
        connection = self._d.registry._connection  # pyright: ignore[reportPrivateUsage]
        try:
            open_sqlite_durable_tenant_operational_authorization(connection)
        except Exception as error:
            raise TenantOperationalUnavailable() from error
        receipt_id = (
            "receipt:"
            + hashlib.sha256(
                json.dumps(
                    {"org": plan.command.org_id, "command": plan.command.command_id},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest()
        )
        # Exact replay is re-authorized against the current post-state but never asks an approver again.
        try:
            stored = connection.execute(
                "SELECT pre_resource_json,post_resource_json,post_resource_fingerprint,pre_resource_fingerprint,evidence_id,approver_subject_id,approval_command_digest,approval_resource_fingerprint,approved_at FROM durable_tenant_operational_authorization_evidence WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
        except Exception as error:
            raise TenantOperationalUnavailable() from error
        if stored is not None:
            post = self._current_post_mirror(plan.command)
            if tuple(stored[:4]) != (
                plan.pre_resource.model_dump_json(),
                post.resource.model_dump_json(),
                post.fingerprint.value,
                plan.pre_fingerprint.value,
            ) or tuple(stored[6:8]) != (plan.command_digest, plan.pre_fingerprint.value):
                raise TenantOperationalUnavailable()
            self._authorize(principal, plan.action, post.resource)
            binding = TenantOperationalAuthorizationBinding(
                plan.pre_resource.model_dump_json(),
                post.resource.model_dump_json(),
                post.fingerprint.value,
                plan.pre_fingerprint.value,
                *tuple(stored[4:]),
            )
            return execute_sqlite_tenant_operational_mutation(
                connection, plan.command, authorization_binding=binding
            )
        # Re-plan closes source/owner/revision drift before approval and immediately before the UoW.
        current = self.plan_mutation(principal, plan.command)
        if current != plan:
            raise TenantOperationalUnavailable()
        evidence = approval.approve(
            principal, plan.action, plan.pre_resource, plan.command_digest, plan.pre_fingerprint
        )
        if type(evidence) is not TenantOperationalApprovalEvidence or (
            evidence.action,
            evidence.command_digest,
            evidence.resource_fingerprint,
        ) != (plan.action, plan.command_digest, plan.pre_fingerprint):
            raise TenantOperationalUnavailable()
        current = self.plan_mutation(principal, plan.command)
        if current != plan:
            raise TenantOperationalUnavailable()
        post = self._planned_post_mirror(plan.command)
        if post.resource != plan.post_resource:
            raise TenantOperationalUnavailable()
        # The final plan/post mirror has no authority to carry an earlier
        # grant across the UoW boundary.  Reauthorize the immutable pre-state
        # immediately before constructing the binding and entering the UoW.
        self._authorize(principal, plan.action, plan.pre_resource)
        binding = TenantOperationalAuthorizationBinding(
            plan.pre_resource.model_dump_json(),
            post.resource.model_dump_json(),
            post.fingerprint.value,
            plan.pre_fingerprint.value,
            evidence.evidence_id,
            evidence.approver_subject_id,
            evidence.command_digest,
            evidence.resource_fingerprint.value,
            evidence.approved_at,
        )
        return execute_sqlite_tenant_operational_mutation(
            connection, plan.command, authorization_binding=binding
        )
