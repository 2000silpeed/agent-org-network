"""Central-only HTTPS Question Gateway composition (ADR 0070).

The Question User MCP artifact is deliberately a remote client.  OIDC token
verification, Registry-backed principal resolution, request ownership lookup,
and Authority checks live here, at the Central Server boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

from fastapi import FastAPI, Header, HTTPException

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    CentralAuthorizer,
    ResourceRef,
)
from agent_org_network.central_oidc_principal import (
    CurrentRegistryOidcPrincipalResolver,
    QuestionGatewayUnavailable,
)
from agent_org_network.oidc import OidcClaims, OidcProvider
from agent_org_network.question_resolution import AskQuestion, QuestionAuthorizationDeniedError
from agent_org_network.question_stream_execution import QuestionStreamRequestNotFoundError
class CurrentOidcPrincipalResolver(Protocol):
    def resolve(self, claims: OidcClaims) -> AuthenticatedPrincipal: ...


class QuestionGatewayApplication(Protocol):
    def ask(self, command: AskQuestion) -> object: ...
    def lookup(self, request_id: str, principal: AuthenticatedPrincipal) -> object: ...


class QuestionRequestOwnerResolver(Protocol):
    """Canonical Question Request ownership lookup for the gateway read boundary."""

    def resolve_question_owner(self, request_id: str) -> ResourceRef | None: ...


@dataclass(frozen=True)
class CentralQuestionGatewayRoutes:
    """All central dependencies required to mount the remote question routes."""

    application: QuestionGatewayApplication
    oidc: OidcProvider
    principals: CurrentOidcPrincipalResolver
    request_owners: QuestionRequestOwnerResolver
    authority: CentralAuthorizer
    render: Callable[[object], str]

    def mount(self, app: FastAPI) -> None:
        mount_https_question_gateway_routes(
            app,
            application=self.application,
            oidc=self.oidc,
            principals=self.principals,
            request_owners=self.request_owners,
            authority=self.authority,
            render=self.render,
        )


def mount_https_question_gateway_routes(
    app: FastAPI,
    *,
    application: QuestionGatewayApplication,
    oidc: OidcProvider,
    principals: CurrentOidcPrincipalResolver,
    request_owners: QuestionRequestOwnerResolver,
    authority: CentralAuthorizer,
    render: Callable[[object], str],
) -> None:
    """Mount the two remote-question routes on the Central Server app.

    Dependencies are injected so the gateway shares the production Central
    process and its authoritative Registry/Authority rather than being bundled
    into the installed Question User MCP client.
    """
    if (
        type(app) is not FastAPI
        or not callable(getattr(application, "ask", None))
        or not callable(getattr(application, "lookup", None))
        or not callable(getattr(oidc, "verify", None))
        or not callable(getattr(principals, "resolve", None))
        or not callable(getattr(request_owners, "resolve_question_owner", None))
        or not callable(getattr(authority, "authorize", None))
        or not callable(getattr(authority, "verify", None))
        or not callable(render)
    ):
        raise QuestionGatewayUnavailable()

    def principal(authorization: str | None) -> AuthenticatedPrincipal:
        if authorization is None or not authorization.startswith("Bearer ") or len(authorization) <= 7:
            raise HTTPException(401, "Authentication required")
        try:
            current = principals.resolve(oidc.verify(authorization[7:]))
            if type(current) is not AuthenticatedPrincipal:
                raise ValueError
            return current
        except Exception:
            raise HTTPException(401, "Authentication required") from None

    def allowed(current: AuthenticatedPrincipal, action: str, resource: ResourceRef) -> bool:
        try:
            grant = authority.authorize(current, cast(object, action), resource)  # type: ignore[arg-type]
            return type(grant) is AuthorizationGrant and authority.verify(
                grant, current, cast(object, action), resource  # type: ignore[arg-type]
            )
        except Exception:
            return False

    @app.post("/ask_org")
    def ask_org(
        body: dict[str, object], authorization: str | None = Header(default=None)
    ) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        if set(body) != {"question"} or type(body.get("question")) is not str:
            raise HTTPException(422, "Invalid request")
        current = principal(authorization)
        if not allowed(current, "question.create", ResourceRef(org_id=current.org_id, kind="question")):
            raise HTTPException(403, "Forbidden")
        try:
            return {"text": render(application.ask(AskQuestion(principal=current, question=cast(str, body["question"]))))}
        except QuestionAuthorizationDeniedError:
            raise HTTPException(403, "Forbidden") from None
        except Exception:
            raise HTTPException(503, "Unavailable") from None

    @app.post("/get_question")
    def get_question(
        body: dict[str, object], authorization: str | None = Header(default=None)
    ) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        if set(body) != {"request_id"} or type(body.get("request_id")) is not str:
            raise HTTPException(422, "Invalid request")
        current = principal(authorization)
        request_id = cast(str, body["request_id"])
        try:
            resource = request_owners.resolve_question_owner(request_id)
        except Exception:
            raise HTTPException(401, "Authentication required") from None
        if (
            type(resource) is not ResourceRef
            or resource.kind != "question"
            or resource.resource_id != request_id
            or resource.owner_subject_id is None
        ):
            raise HTTPException(401, "Authentication required")
        if not allowed(current, "question.read", resource):
            raise HTTPException(403, "Forbidden")
        try:
            return {"text": render(application.lookup(request_id, current))}
        except QuestionStreamRequestNotFoundError:
            raise HTTPException(404, "Not found") from None
        except Exception:
            raise HTTPException(503, "Unavailable") from None

    # FastAPI retains these endpoints; the local reference also makes that
    # ownership explicit to strict static analysis.
    _ = ask_org, get_question


def create_https_question_gateway(
    *,
    application: QuestionGatewayApplication,
    oidc: OidcProvider,
    principals: CurrentOidcPrincipalResolver,
    request_owners: QuestionRequestOwnerResolver,
    authority: CentralAuthorizer,
    render: Callable[[object], str],
) -> FastAPI:
    """Small central-only app factory for focused gateway tests and deployments."""
    app = FastAPI()
    mount_https_question_gateway_routes(
        app,
        application=application,
        oidc=oidc,
        principals=principals,
        request_owners=request_owners,
        authority=authority,
        render=render,
    )
    return app


__all__ = [
    "CurrentOidcPrincipalResolver",
    "CurrentRegistryOidcPrincipalResolver",
    "CentralQuestionGatewayRoutes",
    "QuestionGatewayApplication",
    "QuestionGatewayUnavailable",
    "QuestionRequestOwnerResolver",
    "create_https_question_gateway",
    "mount_https_question_gateway_routes",
]
