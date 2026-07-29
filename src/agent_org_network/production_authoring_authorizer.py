"""Transaction-current production authorization for AuthoringRun writes."""

from __future__ import annotations

import re
import sqlite3
from typing import Protocol

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    AuthorityPolicySnapshot,
    ResourceRef,
    SnapshotCentralAuthorizer,
)
from agent_org_network.production_authoring_identity import (
    AuthoringInvocation,
    ProductionAuthoringIdentityUnavailable,
    ProductionAuthoringIdentityVerifier,
)
from agent_org_network.production_authoring_resource import (
    ProductionAuthoringResourceUnavailable,
    canonical_completion_resource_fingerprint,
    canonical_digest,
    canonical_resource_fingerprint,
    canonical_start_run_id,
    validate_current_authoring_resource,
)
from agent_org_network.sqlite_production_agent_cards import (
    ProductionAgentCardUnavailable,
    validate_production_agent_card_connection,
)
from agent_org_network.sqlite_production_authoring_runs import (
    AuthoringRunCommand,
    AuthoringRunResource,
    BeginAuthoringRunPublishCommand,
    CompleteAuthoringRunCommand,
    CurrentAuthoringRunAuthorization,
    ProductionAuthoringRunDenied,
    StartAuthoringRunCommand,
    ReviewAuthoringRunCommand,
)


class AuthorityPolicySnapshotProvider(Protocol):
    def __call__(self) -> AuthorityPolicySnapshot: ...


def _digest(value: object) -> str:
    return canonical_digest(value)


class ProductionCentralTxCurrentAuthoringAuthorizer:
    def __init__(
        self,
        *,
        policy_snapshot: AuthorityPolicySnapshotProvider,
        central_authorizer: SnapshotCentralAuthorizer,
        identity_verifier: ProductionAuthoringIdentityVerifier,
    ) -> None:
        if (
            not callable(policy_snapshot)
            or type(central_authorizer) is not SnapshotCentralAuthorizer
            or type(identity_verifier) is not ProductionAuthoringIdentityVerifier
        ):
            raise ProductionAuthoringRunDenied()
        self._policy_snapshot = policy_snapshot
        self._central = central_authorizer
        self._identity = identity_verifier
        self._snapshot()

    def _snapshot(self) -> AuthorityPolicySnapshot:
        try:
            snapshot = self._policy_snapshot()
            if (
                type(snapshot) is not AuthorityPolicySnapshot
                or re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
                    snapshot.policy_version,
                )
                is None
            ):
                raise ProductionAuthoringRunDenied()
            return snapshot
        except ProductionAuthoringRunDenied:
            raise
        except Exception as error:
            raise ProductionAuthoringRunDenied() from error

    def current(
        self,
        command: AuthoringRunCommand,
        resource: AuthoringRunResource,
        source_set_digest: str,
        invocation: AuthoringInvocation,
        transaction: sqlite3.Connection,
    ) -> CurrentAuthoringRunAuthorization:
        return self._current(
            command,
            resource,
            source_set_digest,
            invocation,
            transaction,
            allow_unanchored_start=False,
            allow_unanchored_review=False,
        )

    def _current(
        self,
        command: AuthoringRunCommand,
        resource: AuthoringRunResource,
        source_set_digest: str,
        invocation: AuthoringInvocation,
        transaction: sqlite3.Connection,
        *,
        allow_unanchored_start: bool,
        allow_unanchored_review: bool,
    ) -> CurrentAuthoringRunAuthorization:
        before = transaction.total_changes
        try:
            if (
                type(resource) is not AuthoringRunResource
                or type(invocation) is not AuthoringInvocation
                or re.fullmatch(r"[0-9a-f]{64}", source_set_digest) is None
            ):
                raise ProductionAuthoringRunDenied()
            identity = self._identity.current(invocation, transaction)
            org_id, principal_id, agent_id, run_id = self._command_refs(
                command, resource
            )
            if (
                invocation.org_id != org_id
                or invocation.principal_id != principal_id
                or resource.org_id != org_id
                or resource.agent_id != agent_id
                or resource.owner_id != principal_id
            ):
                raise ProductionAuthoringRunDenied()
            validate_production_agent_card_connection(transaction)
            card = transaction.execute(
                "SELECT owner_id,revision,card_digest FROM production_agent_cards "
                "WHERE org_id=? AND agent_id=?",
                (org_id, agent_id),
            ).fetchone()
            if (
                card is None
                or card[0] != resource.owner_id
                or int(card[1]) != resource.card_revision
                or card[2] != resource.card_digest
            ):
                raise ProductionAuthoringRunDenied()
            existing = transaction.execute(
                "SELECT stage,review_outcome,source_set_digest,admitted_bundle_digest "
                "FROM production_authoring_runs "
                "WHERE org_id=? AND run_id=?",
                (org_id, run_id),
            ).fetchone()
            if type(command) is StartAuthoringRunCommand:
                expected_stage = None if existing is None else "extracting"
                allow_absent = True
                idempotency_key: str | None = command.idempotency_key
            else:
                allowed = (
                    {"awaiting_owner_review", "reviewed"}
                    if type(command) is ReviewAuthoringRunCommand
                    else ({"reviewed", "publishing", "published"} if type(command) is BeginAuthoringRunPublishCommand else {"extracting", "awaiting_owner_review"})
                )
                if existing is None or str(existing[0]) not in allowed:
                    raise ProductionAuthoringRunDenied()
                if (
                    type(command) is ReviewAuthoringRunCommand
                    and str(existing[0]) == "reviewed"
                    and not self._exact_review_replay(
                        command,
                        transaction,
                        org_id=org_id,
                        run_id=run_id,
                        outcome=existing[1],
                        source_set_digest=existing[2],
                        admitted_bundle_digest=existing[3],
                        allow_unanchored=allow_unanchored_review,
                    )
                ):
                    raise ProductionAuthoringRunDenied()
                if type(command) is BeginAuthoringRunPublishCommand and (
                    str(existing[0]) == "reviewed" and existing[1] != "Approved"
                ):
                    raise ProductionAuthoringRunDenied()
                expected_stage = str(existing[0])
                allow_absent = False
                idempotency_key = None
            validate_current_authoring_resource(
                transaction,
                org_id=org_id,
                run_id=run_id,
                agent_id=agent_id,
                owner_id=resource.owner_id,
                card_revision=resource.card_revision,
                card_digest=resource.card_digest,
                source_set_digest=source_set_digest,
                allow_absent=allow_absent,
                expected_stage=expected_stage,
                idempotency_key=idempotency_key,
                allow_unanchored=(
                    allow_unanchored_start
                    and type(command) is StartAuthoringRunCommand
                ) or (
                    allow_unanchored_review
                    and type(command) is ReviewAuthoringRunCommand
                ),
                completion=(
                    {
                        "admitted_bundle_digest": command.admitted_bundle_digest,
                        "document_count": command.document_count,
                        "edge_count": command.edge_count,
                        "dropped_count": command.dropped_count,
                        "author_profile_digest": command.author_profile_digest,
                    }
                    if type(command) is CompleteAuthoringRunCommand
                    else (
                        dict(
                            zip(
                                ("admitted_bundle_digest", "document_count", "edge_count", "dropped_count", "author_profile_digest"),
                                transaction.execute(
                                    "SELECT admitted_bundle_digest,document_count,edge_count,dropped_count,author_profile_digest FROM production_authoring_runs WHERE org_id=? AND run_id=?",
                                    (org_id, run_id),
                                ).fetchone() or (),
                                strict=True,
                            )
                    ) if type(command) in (ReviewAuthoringRunCommand, BeginAuthoringRunPublishCommand) else None
                    )
                ),
                require_completion_anchors=not allow_unanchored_start,
            )
            if type(command) is CompleteAuthoringRunCommand:
                fingerprint = canonical_completion_resource_fingerprint(
                    **resource.model_dump(mode="python"),
                    run_id=run_id,
                    source_set_digest=source_set_digest,
                    admitted_bundle_digest=command.admitted_bundle_digest,
                    document_count=command.document_count,
                    edge_count=command.edge_count,
                    dropped_count=command.dropped_count,
                    author_profile_digest=command.author_profile_digest,
                )
            elif type(command) is ReviewAuthoringRunCommand:
                fingerprint = canonical_digest({
                    **resource.model_dump(mode="python"), "run_id": run_id,
                    "source_set_digest": source_set_digest,
                    # The reviewed row is visible before its receipt/audit/outbox
                    # companions are written.  Bind that transient gap to this one
                    # command, rather than admitting another idempotency key with the
                    # same review body.
                    "command_digest": _digest(command.model_dump(mode="json")),
                    "source_digest": command.source_digest,
                    "draft_digest": command.draft_digest,
                    "outcome": command.outcome,
                })
            elif type(command) is BeginAuthoringRunPublishCommand:
                fingerprint = canonical_digest({
                    **resource.model_dump(mode="python"), "run_id": run_id,
                    "source_set_digest": source_set_digest, "review_revision": 2,
                })
            else:
                fingerprint = canonical_resource_fingerprint(
                    **resource.model_dump(mode="python"),
                    run_id=run_id,
                    source_set_digest=source_set_digest,
                )
            snapshot = self._snapshot()
            if (
                snapshot.org_id != org_id
            ):
                raise ProductionAuthoringRunDenied()
            principal = AuthenticatedPrincipal(
                org_id=org_id,
                subject_id=principal_id,
                identity_provider=invocation.identity_provider,
                identity_session_id=invocation.session.value.get_secret_value(),
            )
            authority_resource = ResourceRef(
                org_id=org_id,
                kind="authoring_run",
                resource_id=run_id,
                owner_subject_id=resource.owner_id,
            )
            action = "author.publish" if type(command) in (ReviewAuthoringRunCommand, BeginAuthoringRunPublishCommand) else "author.write"
            grant = self._central.authorize(principal, action, authority_resource)
            if (
                type(grant) is not AuthorizationGrant
                or not self._central.verify(
                    grant, principal, action, authority_resource
                )
                or grant.org_id != org_id
                or grant.subject_id != principal_id
                or grant.action != action
                or grant.resource != authority_resource
                or grant.policy_version != snapshot.policy_version
                or grant.policy_digest != snapshot.content_sha256
            ):
                raise ProductionAuthoringRunDenied()
            return CurrentAuthoringRunAuthorization(
                policy_version=snapshot.policy_version,
                policy_digest=snapshot.content_sha256,
                grant_evidence_digest=_digest(grant.model_dump(mode="json")),
                identity_session_digest=identity.identity_session_digest,
                identity_evidence_digest=identity.identity_evidence_digest,
                resource_fingerprint=fingerprint,
            )
        except ProductionAuthoringRunDenied:
            raise
        except (
            ProductionAuthoringIdentityUnavailable,
            ProductionAgentCardUnavailable,
            ProductionAuthoringResourceUnavailable,
            sqlite3.Error,
            TypeError,
            ValueError,
        ) as error:
            raise ProductionAuthoringRunDenied() from error
        except Exception as error:
            raise ProductionAuthoringRunDenied() from error
        finally:
            if transaction.total_changes != before:
                raise ProductionAuthoringRunDenied()

    def verify_precommit(
        self,
        command: AuthoringRunCommand,
        resource: AuthoringRunResource,
        source_set_digest: str,
        evidence: CurrentAuthoringRunAuthorization,
        invocation: AuthoringInvocation,
        transaction: sqlite3.Connection,
    ) -> bool:
        if type(evidence) is not CurrentAuthoringRunAuthorization:
            return False
        try:
            return self._current(
                command,
                resource,
                source_set_digest,
                invocation,
                transaction,
                allow_unanchored_start=True,
                allow_unanchored_review=True,
            ) == evidence
        except ProductionAuthoringRunDenied:
            return False

    @staticmethod
    def _command_refs(
        command: AuthoringRunCommand,
        resource: AuthoringRunResource,
    ) -> tuple[str, str, str, str]:
        if type(command) is StartAuthoringRunCommand:
            run_id = canonical_start_run_id(
                command.org_id,
                command.agent_id,
                command.idempotency_key,
            )
            return (
                command.org_id,
                command.principal_id,
                command.agent_id,
                run_id,
            )
        if type(command) is CompleteAuthoringRunCommand:
            return (
                command.organization_id,
                command.principal_id,
                resource.agent_id,
                command.run_id,
            )
        if type(command) is ReviewAuthoringRunCommand:
            return (
                command.organization_id,
                command.principal_id,
                resource.agent_id,
                command.run_id,
            )
        if type(command) is BeginAuthoringRunPublishCommand:
            return (
                command.organization_id,
                command.principal_id,
                resource.agent_id,
                command.run_id,
            )
        raise ProductionAuthoringRunDenied()

    @staticmethod
    def _exact_review_replay(
        command: ReviewAuthoringRunCommand,
        transaction: sqlite3.Connection,
        *,
        org_id: str,
        run_id: str,
        outcome: object,
        source_set_digest: object,
        admitted_bundle_digest: object,
        allow_unanchored: bool,
    ) -> bool:
        """Allow Reviewed only for its exact durable receipt (or precommit gap)."""
        if (
            command.source_digest != source_set_digest
            or command.draft_digest != admitted_bundle_digest
            or command.outcome != outcome
        ):
            return False
        command_digest = _digest(command.model_dump(mode="json"))
        receipt = transaction.execute(
            "SELECT command_digest,action_kind,result_revision,result_state "
            "FROM production_authoring_command_receipts "
            "WHERE org_id=? AND idempotency_key=?",
            (org_id, command.idempotency_key),
        ).fetchone()
        if receipt is not None:
            return (
                receipt[0] == command_digest
                and receipt[1] == "authoring_run.review"
                and int(receipt[2]) == 2
                and receipt[3] == "reviewed"
            )
        if not allow_unanchored:
            return False
        return not any(
            transaction.execute(
                f"SELECT 1 FROM {table} WHERE org_id=? AND run_id=? "  # noqa: S608
                f"AND {column}=?",
                (org_id, run_id, value),
            ).fetchone()
            for table, column, value in (
                ("production_authoring_command_receipts", "action_kind", "authoring_run.review"),
                ("production_authoring_audit_intents", "event_kind", "run_reviewed"),
                ("production_authoring_outbox_intents", "kind", "authoring.run_reviewed"),
            )
        )


__all__ = [
    "AuthorityPolicySnapshotProvider",
    "ProductionCentralTxCurrentAuthoringAuthorizer",
]
