"""Production HTTP boundary for Card Owner installation pairing."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from agent_org_network.central_owner_pairing_issue import (
    CentralOwnerPairingIssueConflict,
    CentralOwnerPairingIssueStore,
    CentralOwnerPairingIssueUnavailable,
    IssueOwnerPairingCommand,
    ProductionCentralPairingIssueAuthorizer,
    RedeemOwnerPairingCommand,
)
from agent_org_network.central_authoring_web import create_central_authoring_app
from agent_org_network.central_question_gateway import CentralQuestionGatewayRoutes
from agent_org_network.production_authoring_authorizer import (
    ProductionCentralTxCurrentAuthoringAuthorizer,
)
from agent_org_network.owner_credential_envelope import (
    OwnerCredentialEnvelopeUnavailable,
    X25519PublicJwk,
)
from agent_org_network.production_authoring_identity import (
    AuthoringIdentitySessionRef,
    AuthoringInvocation,
)
from agent_org_network.production_identity_sessions import (
    ProductionIdentityUnavailable,
    ProductionPrincipalResolver,
)
from agent_org_network.sqlite_production_authoring_runs import (
    SqliteProductionAuthoringRuns,
)

_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MAX_BODY = 16 * 1024
_NO_SECRET_HEADERS = {"cache-control": "no-store", "pragma": "no-cache"}


async def _bounded_json(request: Request) -> object:
    if request.headers.get("content-type") != "application/json":
        raise ValueError
    length = request.headers.get("content-length")
    if length is None or not length.isdigit() or not 1 <= int(length) <= _MAX_BODY:
        raise ValueError
    declared = int(length)
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > declared or received > _MAX_BODY:
            raise ValueError
        chunks.append(chunk)
    if received != declared:
        raise ValueError
    return json.loads(b"".join(chunks))


class _IssueBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agent_id: str
    expected_card_revision: int
    expected_card_digest: str
    device_class: str


class _RedeemBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent_id: str
    pairing_code: SecretStr
    device_public_key: X25519PublicJwk


def mount_central_owner_pairing_routes(
    app: FastAPI,
    *,
    pairing: CentralOwnerPairingIssueStore,
    principal_resolver: ProductionPrincipalResolver,
) -> None:
    if (
        type(app) is not FastAPI
        or type(pairing) is not CentralOwnerPairingIssueStore
        or type(principal_resolver) is not ProductionPrincipalResolver
    ):
        raise CentralOwnerPairingIssueUnavailable()

    @app.middleware("http")
    async def no_secret_cache(  # pyright: ignore[reportUnusedFunction]
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["cache-control"] = "no-store"
        response.headers["pragma"] = "no-cache"
        return response

    def invocation(session: str | None) -> AuthoringInvocation:
        if session is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        try:
            principal = principal_resolver.resolve(session).principal
            return AuthoringInvocation(
                session=AuthoringIdentitySessionRef(value=SecretStr(session)),
                org_id=principal.org_id,
                principal_id=principal.subject_id,
                identity_provider=principal.identity_provider,
            )
        except (ProductionIdentityUnavailable, ValueError):
            raise HTTPException(
                status_code=401, detail="Authentication required"
            ) from None

    @app.post("/pairing/owner/issue")
    async def issue(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        idempotency_key: Annotated[str | None, Header()] = None,
        aon_identity_session: Annotated[str | None, Cookie()] = None,
    ) -> Response:
        current = invocation(aon_identity_session)
        if idempotency_key is None or _REF.fullmatch(idempotency_key) is None:
            raise HTTPException(status_code=422, detail="Invalid request")
        try:
            body = _IssueBody.model_validate(await _bounded_json(request))
            result = pairing.issue(
                IssueOwnerPairingCommand(
                    org_id=current.org_id,
                    principal_id=current.principal_id,
                    idempotency_key=idempotency_key,
                    **body.model_dump(),
                ),
                current,
            )
            payload = result.model_dump(mode="json")
            payload["pairing_code"] = result.pairing_code.get_secret_value()
            payload.pop("replayed")
            return Response(
                content=json.dumps(payload, separators=(",", ":")),
                media_type="application/json",
                headers=_NO_SECRET_HEADERS,
            )
        except CentralOwnerPairingIssueConflict:
            raise HTTPException(status_code=409, detail="Conflict") from None
        except CentralOwnerPairingIssueUnavailable:
            raise HTTPException(status_code=503, detail="Unavailable") from None
        except (
            OwnerCredentialEnvelopeUnavailable,
            ValidationError,
            ValueError,
            json.JSONDecodeError,
        ):
            raise HTTPException(status_code=422, detail="Invalid request") from None

    @app.post("/pairing/owner/redeem")
    async def redeem(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> Response:
        if idempotency_key is None or _REF.fullmatch(idempotency_key) is None:
            raise HTTPException(status_code=422, detail="Invalid request")
        try:
            body = _RedeemBody.model_validate(await _bounded_json(request))
            result = pairing.redeem(
                RedeemOwnerPairingCommand(
                    idempotency_key=idempotency_key, **body.model_dump()
                )
            )
            payload = {
                "audience": "owner-install",
                "credential_id": result.credential_id,
                "org_id": result.evidence.org_id,
                "owner_id": result.evidence.owner_id,
                "agent_id": result.evidence.agent_id,
                "card_revision": result.evidence.card_revision,
                "card_digest": result.evidence.card_digest,
                "pairing_intent_digest": result.pairing_intent_digest,
                "issue_receipt_id": result.issue_receipt_id,
                "issue_receipt_digest": result.issue_receipt_digest,
                "redeem_receipt_id": result.redeem_receipt_id,
                "redeem_receipt_digest": result.redeem_receipt_digest,
                "identity_provider": result.evidence.identity_provider,
                "device_key_thumbprint": result.envelope.aad.device_key_thumbprint,
                "credential_generation": result.envelope.aad.credential_generation,
                "expires_at": result.envelope.aad.expires_at,
                "envelope": result.envelope.model_dump(mode="json"),
            }
            return Response(
                content=json.dumps(payload, separators=(",", ":")),
                media_type="application/json",
                headers=_NO_SECRET_HEADERS,
            )
        except CentralOwnerPairingIssueConflict:
            raise HTTPException(status_code=409, detail="Conflict") from None
        except CentralOwnerPairingIssueUnavailable:
            raise HTTPException(status_code=503, detail="Unavailable") from None
        except (
            OwnerCredentialEnvelopeUnavailable,
            ValidationError,
            ValueError,
            json.JSONDecodeError,
        ):
            raise HTTPException(status_code=422, detail="Invalid request") from None


def create_central_owner_pairing_app(
    *,
    pairing: CentralOwnerPairingIssueStore,
    principal_resolver: ProductionPrincipalResolver,
) -> FastAPI:
    app = FastAPI()
    mount_central_owner_pairing_routes(
        app, pairing=pairing, principal_resolver=principal_resolver
    )
    return app


def create_production_central_authoring_pairing_app(
    *,
    runs: SqliteProductionAuthoringRuns,
    authoring_authorizer: ProductionCentralTxCurrentAuthoringAuthorizer,
    pairing: CentralOwnerPairingIssueStore,
    pairing_authorizer: ProductionCentralPairingIssueAuthorizer,
    principal_resolver: ProductionPrincipalResolver,
    question_gateway: CentralQuestionGatewayRoutes | None = None,
) -> FastAPI:
    if (
        type(pairing_authorizer) is not ProductionCentralPairingIssueAuthorizer
        or getattr(pairing, "_authorizer", None) is not pairing_authorizer
    ):
        raise CentralOwnerPairingIssueUnavailable()
    app = create_central_authoring_app(
        runs=runs,
        principal_resolver=principal_resolver,
        authorizer=authoring_authorizer,
        include_publish_begin=False,
    )
    mount_central_owner_pairing_routes(
        app, pairing=pairing, principal_resolver=principal_resolver
    )
    if question_gateway is not None:
        if type(question_gateway) is not CentralQuestionGatewayRoutes:
            raise CentralOwnerPairingIssueUnavailable()
        question_gateway.mount(app)
    return app


__all__ = [
    "create_central_owner_pairing_app",
    "create_production_central_authoring_pairing_app",
    "mount_central_owner_pairing_routes",
]
