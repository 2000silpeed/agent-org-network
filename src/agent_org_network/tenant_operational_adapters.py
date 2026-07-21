"""Tenant-only HTTP/MCP adapters; they never reuse legacy operational paths.

The mutation entry points are deliberately a separate, capability-gated
composition from the R2b read surface.  In particular, they do not turn a
partially wired read application into a write application.
"""
# pyright: reportUnusedFunction=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
import sqlite3
from typing import Any, Literal, cast

from fastapi import FastAPI, HTTPException, Request
from mcp.server.fastmcp import FastMCP

from agent_org_network.central_authority import AuthenticatedPrincipal, ResourceRef
from agent_org_network.tenant_operational_application import (
    TenantOperationalApplication,
    TenantOperationalMutationPlan,
    TenantOperationalMutationUnavailable,
    TenantOperationalUnavailable,
)
from agent_org_network.tenant_operational_approval import TenantOperationalApprovalPort
from agent_org_network.sqlite_durable_tenant_operational_authorization import (
    open_sqlite_durable_tenant_operational_authorization,
)
from agent_org_network.sqlite_durable_tenant_operational_mutations import (
    capture_sqlite_tenant_operational_mutation_scope_snapshot,
    open_sqlite_durable_tenant_operational_mutations,
)
from agent_org_network.sqlite_tenant_operational_mutation_uow import (
    CardRegisterCommand,
    CardTransferOwnerCommand,
    HitlWriteCommand,
    SessionEndCommand,
    TenantOperationalMutationReceipt,
    canonical_tenant_operational_mutation_uow_command_digest,
)
from agent_org_network.tenant_operational_ports import ResourceFingerprint


_MUTATION_UNAVAILABLE = "operational_mutation_uow_unavailable"
_MUTATION_FAILED = "operational_mutation_unavailable"
_CANONICAL_COMMAND_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)


class TenantOperationalMutationTransportUnavailable(TenantOperationalUnavailable):
    """The exact R1 transport composition is not currently usable."""

    code = _MUTATION_UNAVAILABLE


@dataclass(frozen=True)
class SealedTenantOperationalApprovalProvider:
    """An explicit transport seal around the R1.2 approval port.

    A bare callable/protocol is intentionally not accepted by mutation
    factories: that would let a channel accidentally compose an unreviewed
    approval callback.  The provider's output is still verified by the
    application immediately before the R1.1 transaction.
    """

    port: TenantOperationalApprovalPort

    def approve(self, *args: object, **kwargs: object) -> object:
        return self.port.approve(*args, **kwargs)  # type: ignore[arg-type]


@dataclass(frozen=True)
class TenantOperationalMutationTransport:
    """Exact tenant mutation composition used by both HTTP and MCP.

    ``application`` already owns the six exact tenant source adapters and
    central authorization.  This object additionally validates the R1.0
    durable receipt capability, R1.2 authorization-evidence capability, and
    the R1.1 executable UoW against that same SQLite connection.
    """

    application: TenantOperationalApplication
    principal_provider: Callable[[], AuthenticatedPrincipal]
    approval: SealedTenantOperationalApprovalProvider

    def __post_init__(self) -> None:
        if (
            type(self.application) is not TenantOperationalApplication
            or not callable(self.principal_provider)
            or type(self.approval) is not SealedTenantOperationalApprovalProvider
        ):
            raise TypeError("exact tenant mutation transport dependencies가 필요합니다.")

    def _connection(self) -> sqlite3.Connection:
        # The application constructor owns the exact six-source aggregate.
        connection = self.application._d.registry._connection  # pyright: ignore[reportPrivateUsage]
        if type(connection) is not sqlite3.Connection:
            raise TenantOperationalMutationTransportUnavailable()
        return connection

    def validate_only(self) -> sqlite3.Connection:
        """Check every write prerequisite before reading a body or principal."""
        try:
            connection = self._connection()
            open_sqlite_durable_tenant_operational_mutations(connection).validate_only()
            open_sqlite_durable_tenant_operational_authorization(connection)
            # R1.1 is reached only through TenantOperationalApplication's
            # plan→approval→commit boundary; this adapter never calls its
            # execute function directly.
            capture_sqlite_tenant_operational_mutation_scope_snapshot(connection)
            return connection
        except TenantOperationalMutationTransportUnavailable:
            raise
        except Exception as error:
            raise TenantOperationalMutationTransportUnavailable() from error

    def principal(self) -> AuthenticatedPrincipal:
        try:
            value = self.principal_provider()
            if type(value) is not AuthenticatedPrincipal:
                raise TypeError
            return AuthenticatedPrincipal.model_validate(value, strict=True)
        except Exception as error:
            raise TenantOperationalMutationTransportUnavailable() from error

    @staticmethod
    def _canonical_created_at(value: object) -> str:
        """Accept only a channel-stable millisecond UTC command timestamp."""
        if type(value) is not str or _CANONICAL_COMMAND_TIMESTAMP.fullmatch(value) is None:
            raise TenantOperationalUnavailable()
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError as error:
            raise TenantOperationalUnavailable() from error
        return value

    def commit(
        self,
        kind: Literal["register", "transfer", "session", "hitl"],
        *,
        command_id: str,
        target_id: str,
        owner_id: str | None = None,
        on: bool | None = None,
        created_at: str | None = None,
    ) -> TenantOperationalMutationReceipt:
        connection = self.validate_only()
        principal = self.principal()
        if type(command_id) is not str or not command_id:
            raise TenantOperationalUnavailable()
        timestamp = self._canonical_created_at(created_at)
        org_id = self.application._d.org.value  # pyright: ignore[reportPrivateUsage]
        scope = capture_sqlite_tenant_operational_mutation_scope_snapshot(connection)
        try:
            if kind == "register":
                if type(owner_id) is not str:
                    raise ValueError
                command = CardRegisterCommand(org_id, command_id, principal.subject_id, scope, timestamp, target_id, owner_id)
            elif kind == "transfer":
                if type(owner_id) is not str:
                    raise ValueError
                command = CardTransferOwnerCommand(org_id, command_id, principal.subject_id, scope, timestamp, target_id, owner_id)
            elif kind == "session":
                command = SessionEndCommand(org_id, command_id, principal.subject_id, scope, timestamp, target_id)
            else:
                if type(on) is not bool:
                    raise ValueError
                command = HitlWriteCommand(org_id, command_id, principal.subject_id, scope, timestamp, target_id, on)
        except (TypeError, ValueError) as error:
            raise TenantOperationalUnavailable() from error
        action = cast(Literal["card.register", "card.transfer_owner", "session.end", "hitl.write"], {
            "register": "card.register",
            "transfer": "card.transfer_owner",
            "session": "session.end",
            "hitl": "hitl.write",
        }[kind])
        # A replay crosses channels with a newly captured source snapshot.
        # Reconstruct only the already persisted, sealed pre-plan after the
        # immutable R1.1 semantic command identity matches exactly.  The
        # application then reauthorizes the live post-state before returning
        # the receipt, without consulting the approval provider again.
        stored = connection.execute(
            "SELECT principal_id,action,command_digest FROM durable_tenant_operational_mutation_receipts WHERE org_id=? AND command_id=?",
            (org_id, command_id),
        ).fetchone()
        if stored is not None:
            if tuple(stored) != (
                principal.subject_id,
                action,
                canonical_tenant_operational_mutation_uow_command_digest(command),
            ):
                raise TenantOperationalUnavailable()
            evidence = connection.execute(
                "SELECT pre_resource_json,post_resource_json,pre_resource_fingerprint,approval_command_digest FROM durable_tenant_operational_authorization_evidence WHERE receipt_id=?",
                (
                    "receipt:"
                    + hashlib.sha256(
                        json.dumps(
                            {"org": org_id, "command": command_id},
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                ),
            ).fetchone()
            if evidence is None:
                raise TenantOperationalUnavailable()
            pre, post, fingerprint, approval_digest = evidence
            try:
                plan = TenantOperationalMutationPlan(
                    command,
                    action,
                    ResourceRef.model_validate_json(pre, strict=True),
                    ResourceRef.model_validate_json(post, strict=True),
                    ResourceFingerprint(fingerprint),
                    approval_digest,
                )
            except Exception as error:
                raise TenantOperationalUnavailable() from error
        else:
            plan = self.application.plan_mutation(principal, command)
        return self.application.approve_and_commit_mutation(principal, plan, self.approval)  # type: ignore[arg-type]


def _dto(value: object) -> dict[str, Any]:
    """HTTP와 MCP가 공유하는 secret-free strict tenant DTO projection."""
    from agent_org_network.tenant_operational_ports import (
        SafeAuditEvent,
        TenantCard,
        TenantSession,
    )

    if type(value) is TenantCard:
        return {
            "card_id": value.card_id,
            "owner_id": value.owner_id,
            "fingerprint": value.fingerprint.value,
        }
    if type(value) is TenantSession:
        return {
            "session_id": value.session_id,
            "user_id": value.user_id,
            "status": value.status,
            "fingerprint": value.fingerprint.value,
        }
    if type(value) is SafeAuditEvent:
        return {
            "action": value.action,
            "subject_id": value.subject_id,
            "outcome": value.outcome,
            "fingerprint": value.fingerprint.value,
        }
    raise TenantOperationalUnavailable()


def _receipt_dto(receipt: TenantOperationalMutationReceipt) -> dict[str, object]:
    return {
        "receipt_id": receipt.receipt_id,
        "command_id": receipt.command_id,
        "action": receipt.action,
        "replayed": receipt.replayed,
    }


def create_tenant_operational_http_app(
    *,
    application: TenantOperationalApplication,
    principal_provider: Callable[[], AuthenticatedPrincipal],
    mutation_transport: TenantOperationalMutationTransport | None = None,
) -> FastAPI:
    if type(application) is not TenantOperationalApplication or not callable(principal_provider):
        raise TypeError("tenant application과 server-side principal provider가 필요합니다.")
    app = FastAPI()

    def principal() -> AuthenticatedPrincipal:
        value = principal_provider()
        if type(value) is not AuthenticatedPrincipal:
            raise TenantOperationalUnavailable()
        return value

    def invoke(operation: Callable[[], object]) -> object:
        try:
            return operation()
        except TenantOperationalMutationUnavailable as error:
            raise HTTPException(503, detail=error.code) from error
        except TenantOperationalUnavailable as error:
            raise HTTPException(503, detail="operational_unavailable") from error

    def mutation_invoke(operation: Callable[[], TenantOperationalMutationReceipt]) -> dict[str, object]:
        try:
            receipt = operation()
            return _receipt_dto(receipt)
        except TenantOperationalMutationTransportUnavailable as error:
            raise HTTPException(503, detail=error.code) from error
        except TenantOperationalUnavailable as error:
            raise HTTPException(503, detail=_MUTATION_FAILED) from error

    @app.get("/tenant-operational/cards")
    def cards() -> list[dict[str, Any]]:
        return [_dto(x) for x in invoke(lambda: application.cards(principal()))]  # type: ignore[union-attr]

    @app.get("/tenant-operational/cards/{card_id}")
    def card(card_id: str) -> dict[str, Any]:
        return _dto(invoke(lambda: application.card(principal(), card_id)))

    @app.get("/tenant-operational/graph")
    def graph() -> list[dict[str, Any]]:
        return [_dto(x) for x in invoke(lambda: application.graph(principal()))]  # type: ignore[union-attr]

    @app.get("/tenant-operational/sessions/{session_id}")
    def session(session_id: str) -> dict[str, Any]:
        return _dto(invoke(lambda: application.session(principal(), session_id)))

    @app.get("/tenant-operational/audit")
    def audit() -> list[dict[str, Any]]:
        return [_dto(x) for x in invoke(lambda: application.audit(principal()))]  # type: ignore[union-attr]

    @app.get("/tenant-operational/audit/{sequence}")
    def audit_detail(sequence: int) -> dict[str, Any]:
        return _dto(invoke(lambda: application.audit_detail(principal(), sequence)))

    @app.get("/tenant-operational/hitl/{card_id}")
    def hitl(card_id: str) -> dict[str, bool]:
        return {"on": bool(invoke(lambda: application.hitl(principal(), card_id)))}

    # R1 capability is checked before parsing body or resolving the principal.
    # Keeping the legacy-shaped routes here preserves the explicit 503 seam
    # when a caller composes only the read application.
    async def body_after_capability(request: Request) -> dict[str, object]:
        if mutation_transport is None:
            invoke(application.mutation_unavailable)
        assert mutation_transport is not None
        try:
            mutation_transport.validate_only()
        except TenantOperationalMutationTransportUnavailable as error:
            raise HTTPException(503, detail=error.code) from error
        try:
            raw = await request.json()
        except Exception as error:
            raise HTTPException(400, detail="operational_mutation_invalid") from error
        if type(raw) is not dict:
            raise HTTPException(400, detail="operational_mutation_invalid")
        return cast(dict[str, object], raw)

    @app.post("/tenant-operational/cards")
    async def register_card(request: Request) -> dict[str, object]:
        body = await body_after_capability(request)
        if mutation_transport is None:
            raise AssertionError("unreachable")
        transport = mutation_transport
        return mutation_invoke(
            lambda: transport.commit(
                "register",
                command_id=cast(str, body.get("command_id")),
                target_id=cast(str, body.get("card_id")),
                owner_id=cast(str | None, body.get("owner_id")),
                created_at=cast(str | None, body.get("created_at")),
            )
        )

    @app.post("/tenant-operational/cards/{card_id}/transfer")
    async def transfer_card_owner(card_id: str, request: Request) -> dict[str, object]:
        body = await body_after_capability(request)
        if mutation_transport is None:
            raise AssertionError("unreachable")
        transport = mutation_transport
        return mutation_invoke(
            lambda: transport.commit(
                "transfer", command_id=cast(str, body.get("command_id")), target_id=card_id,
                owner_id=cast(str | None, body.get("owner_id")), created_at=cast(str | None, body.get("created_at")),
            )
        )

    @app.post("/tenant-operational/sessions/{session_id}/end")
    async def end_session(session_id: str, request: Request) -> dict[str, object]:
        body = await body_after_capability(request)
        if mutation_transport is None:
            raise AssertionError("unreachable")
        transport = mutation_transport
        return mutation_invoke(
            lambda: transport.commit(
                "session", command_id=cast(str, body.get("command_id")), target_id=session_id,
                created_at=cast(str | None, body.get("created_at")),
            )
        )

    @app.post("/tenant-operational/hitl/{card_id}")
    async def set_hitl(card_id: str, request: Request) -> dict[str, object]:
        body = await body_after_capability(request)
        if mutation_transport is None:
            raise AssertionError("unreachable")
        transport = mutation_transport
        return mutation_invoke(
            lambda: transport.commit(
                "hitl", command_id=cast(str, body.get("command_id")), target_id=card_id,
                on=cast(bool | None, body.get("on")), created_at=cast(str | None, body.get("created_at")),
            )
        )

    # This pre-R1 placeholder remains deliberately closed: audit append is
    # not one of the R1.1 sealed commands and must never become a side door.
    @app.post("/tenant-operational/audit")
    def append_audit_placeholder() -> None:
        invoke(application.mutation_unavailable)

    return app


def create_tenant_operational_mcp_server(
    *,
    application: TenantOperationalApplication,
    principal_provider: Callable[[], AuthenticatedPrincipal],
    mutation_transport: TenantOperationalMutationTransport | None = None,
) -> FastMCP:
    if type(application) is not TenantOperationalApplication or not callable(principal_provider):
        raise TypeError("tenant application과 server-side principal provider가 필요합니다.")
    mcp = FastMCP("Agent Org Network — tenant operational")

    def principal() -> AuthenticatedPrincipal:
        value = principal_provider()
        if type(value) is not AuthenticatedPrincipal:
            raise TenantOperationalUnavailable()
        return value

    def invoke(operation: Callable[[], object]) -> object:
        try:
            return operation()
        except TenantOperationalMutationUnavailable as error:
            return {"code": error.code}
        except TenantOperationalUnavailable:
            return {"code": "operational_unavailable"}

    def mutate(operation: Callable[[], TenantOperationalMutationReceipt]) -> dict[str, object]:
        try:
            return _receipt_dto(operation())
        except TenantOperationalMutationTransportUnavailable as error:
            return {"code": error.code}
        except TenantOperationalUnavailable:
            return {"code": _MUTATION_FAILED}

    @mcp.tool()
    def tenant_cards() -> list[dict[str, Any]]:
        value = invoke(lambda: application.cards(principal()))
        if type(value) is dict:
            return [value]
        if isinstance(value, tuple):
            return [_dto(item) for item in value]
        raise TenantOperationalUnavailable()

    @mcp.tool()
    def tenant_card(card_id: str) -> dict[str, Any]:
        value = invoke(lambda: application.card(principal(), card_id))
        return value if type(value) is dict else _dto(value)

    @mcp.tool()
    def tenant_graph() -> list[dict[str, Any]]:
        value = invoke(lambda: application.graph(principal()))
        if type(value) is dict:
            return [value]
        if isinstance(value, tuple):
            return [_dto(item) for item in value]
        raise TenantOperationalUnavailable()

    @mcp.tool()
    def tenant_session(session_id: str) -> dict[str, Any]:
        value = invoke(lambda: application.session(principal(), session_id))
        return value if type(value) is dict else _dto(value)

    @mcp.tool()
    def tenant_audit() -> list[dict[str, Any]]:
        value = invoke(lambda: application.audit(principal()))
        if type(value) is dict:
            return [value]
        if isinstance(value, tuple):
            return [_dto(item) for item in value]
        raise TenantOperationalUnavailable()

    @mcp.tool()
    def tenant_audit_detail(sequence: int) -> dict[str, Any]:
        value = invoke(lambda: application.audit_detail(principal(), sequence))
        return value if type(value) is dict else _dto(value)

    @mcp.tool()
    def tenant_hitl(card_id: str) -> dict[str, bool]:
        value = invoke(lambda: application.hitl(principal(), card_id))
        return {"on": value} if type(value) is bool else value  # type: ignore[return-value]

    def unavailable() -> dict[str, object]:
        if mutation_transport is None:
            return {"code": _MUTATION_UNAVAILABLE}
        try:
            mutation_transport.validate_only()
        except TenantOperationalMutationTransportUnavailable as error:
            return {"code": error.code}
        return {}

    @mcp.tool()
    def tenant_register_card(
        command_id: str = "", card_id: str = "", owner_id: str = "", created_at: str | None = None
    ) -> dict[str, object] | str:
        if mutation_transport is None:
            return _MUTATION_UNAVAILABLE
        blocked = unavailable()
        if blocked:
            return blocked
        assert mutation_transport is not None
        transport = mutation_transport
        return mutate(lambda: transport.commit("register", command_id=command_id, target_id=card_id, owner_id=owner_id, created_at=created_at))

    @mcp.tool()
    def tenant_transfer_card_owner(
        command_id: str = "", card_id: str = "", owner_id: str = "", created_at: str | None = None
    ) -> dict[str, object] | str:
        if mutation_transport is None:
            return _MUTATION_UNAVAILABLE
        blocked = unavailable()
        if blocked:
            return blocked
        assert mutation_transport is not None
        transport = mutation_transport
        return mutate(lambda: transport.commit("transfer", command_id=command_id, target_id=card_id, owner_id=owner_id, created_at=created_at))

    @mcp.tool()
    def tenant_end_session(
        command_id: str = "", session_id: str = "", created_at: str | None = None
    ) -> dict[str, object] | str:
        if mutation_transport is None:
            return _MUTATION_UNAVAILABLE
        blocked = unavailable()
        if blocked:
            return blocked
        assert mutation_transport is not None
        transport = mutation_transport
        return mutate(lambda: transport.commit("session", command_id=command_id, target_id=session_id, created_at=created_at))

    @mcp.tool()
    def tenant_set_hitl(
        command_id: str = "", card_id: str = "", on: bool = False, created_at: str | None = None
    ) -> dict[str, object] | str:
        if mutation_transport is None:
            return _MUTATION_UNAVAILABLE
        blocked = unavailable()
        if blocked:
            return blocked
        assert mutation_transport is not None
        transport = mutation_transport
        return mutate(lambda: transport.commit("hitl", command_id=command_id, target_id=card_id, on=on, created_at=created_at))

    @mcp.tool()
    def tenant_append_audit() -> str:
        # Kept as the explicit, permanently unavailable pre-R1 placeholder.
        return _MUTATION_UNAVAILABLE

    return mcp


def create_tenant_operational_mutation_http_app(
    *, transport: TenantOperationalMutationTransport
) -> FastAPI:
    """Create the R1.3 mutation-enabled tenant HTTP surface only."""
    if type(transport) is not TenantOperationalMutationTransport:
        raise TypeError("exact tenant mutation transport가 필요합니다.")
    return create_tenant_operational_http_app(
        application=transport.application,
        principal_provider=transport.principal_provider,
        mutation_transport=transport,
    )


def create_tenant_operational_mutation_mcp_server(
    *, transport: TenantOperationalMutationTransport
) -> FastMCP:
    """Create the R1.3 mutation-enabled tenant MCP surface only."""
    if type(transport) is not TenantOperationalMutationTransport:
        raise TypeError("exact tenant mutation transport가 필요합니다.")
    return create_tenant_operational_mcp_server(
        application=transport.application,
        principal_provider=transport.principal_provider,
        mutation_transport=transport,
    )
