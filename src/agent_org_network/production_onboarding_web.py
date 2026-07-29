"""Production-only Registry User onboarding HTTP composition."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Protocol, cast

from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict

from agent_org_network.production_identity_sessions import (
    ProductionAuthenticatedIdentity,
    ProductionIdentityUnavailable,
    ProductionPrincipalResolver,
)
from agent_org_network.production_oidc_login import (
    AuthorizationCodeOidcProvider,
    OidcLoginTransactions,
    ProductionOidcLoginUnavailable,
    authorization_url,
)
from agent_org_network.sqlite_production_agent_cards import (
    ProductionAgentCardCommand,
    ProductionAgentCardConflict,
    ProductionAgentCardDenied,
    ProductionAgentCardRevisionConflict,
    ProductionAgentCardUnavailable,
    SqliteProductionAgentCards,
    TxCurrentCardRegistrationAuthorizer,
)
from agent_org_network.sqlite_production_registry_users import (
    ProductionRegistryUserCommand,
    ProductionRegistryUserConflict,
    ProductionRegistryUserDenied,
    ProductionRegistryUserRevisionConflict,
    ProductionRegistryUserUnavailable,
    SqliteProductionRegistryUsers,
)


class _RegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int
    user_id: str
    email: str
    manager: str | None = None


class _CardRegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int
    agent_id: str
    owner: str
    team: str
    summary: str
    domains: list[str]
    maintainer: str | None = None
    can_answer: list[str] = []
    cannot_answer: list[str] = []
    approval_when: list[str] = []
    collaborate_when: list[str] = []
    knowledge_sources: list[str] = []
    trust_labels: list[str] = []


class CardRegistrationDelegationAuthorizer(Protocol):
    def allows(
        self,
        *,
        identity: ProductionAuthenticatedIdentity,
        org_id: str,
        agent_id: str,
        owner_id: str,
    ) -> bool: ...


def create_production_onboarding_app(
    *,
    users: SqliteProductionRegistryUsers | None,
    principal_resolver: ProductionPrincipalResolver | None,
    agent_cards: SqliteProductionAgentCards | None = None,
    card_authorizer: TxCurrentCardRegistrationAuthorizer | None = None,
    card_list_all_authorized: Callable[[ProductionAuthenticatedIdentity], bool] | None = None,
    registration_delegation_authorizer: CardRegistrationDelegationAuthorizer | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    oidc_provider: AuthorizationCodeOidcProvider | None = None,
    oidc_transactions: OidcLoginTransactions | None = None,
    oidc_authorization_url: str | None = None,
    oidc_client_id: str | None = None,
    oidc_redirect_uri: str | None = None,
) -> FastAPI:
    app = FastAPI()
    capable = (
        type(users) is SqliteProductionRegistryUsers
        and type(principal_resolver) is ProductionPrincipalResolver
    )
    oidc_capable = (
        oidc_provider is not None
        and type(oidc_transactions) is OidcLoginTransactions
        and oidc_authorization_url is not None
        and oidc_client_id is not None
        and oidc_redirect_uri is not None
    )
    card_capable = (
        type(agent_cards) is SqliteProductionAgentCards
        and card_authorizer is not None
        and getattr(agent_cards, "_authorize", None) is card_authorizer
    )

    def require_oidc() -> tuple[AuthorizationCodeOidcProvider, OidcLoginTransactions]:
        if not capable or not oidc_capable:
            raise HTTPException(status_code=503, detail="SSO unavailable")
        assert oidc_provider is not None and oidc_transactions is not None
        return oidc_provider, oidc_transactions

    @app.get("/auth/oidc/start")
    def oidc_start() -> Response:  # pyright: ignore[reportUnusedFunction]
        _, transactions = require_oidc()
        assert oidc_authorization_url is not None
        assert oidc_client_id is not None
        assert oidc_redirect_uri is not None
        transaction = transactions.begin()
        response = RedirectResponse(
            authorization_url(
                endpoint=oidc_authorization_url,
                client_id=oidc_client_id,
                redirect_uri=oidc_redirect_uri,
                transaction=transaction,
            ),
            status_code=302,
        )
        response.set_cookie(
            "aon_oidc_transaction",
            transaction.browser_binding,
            max_age=300,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/auth/oidc/callback",
        )
        return response

    @app.get("/auth/oidc/callback")
    def oidc_callback(  # pyright: ignore[reportUnusedFunction]
        state: str | None = None,
        code: str | None = None,
        error: str | None = None,
        aon_oidc_transaction: Annotated[str | None, Cookie()] = None,
    ) -> Response:
        provider, transactions = require_oidc()
        transaction = None
        try:
            if state is None or aon_oidc_transaction is None:
                raise ProductionOidcLoginUnavailable()
            # A provider error is still a callback attempt: consume a valid
            # transaction before returning the generic failure.
            transaction = transactions.consume(
                state=state, browser_binding=aon_oidc_transaction
            )
            if error is not None or code is None:
                raise ProductionOidcLoginUnavailable()
            assert oidc_redirect_uri is not None
            assert principal_resolver is not None
            proof = provider.exchange(
                code=code,
                redirect_uri=oidc_redirect_uri,
                code_verifier=transaction.code_verifier,
                expected_nonce=transaction.nonce,
            )
            current = principal_resolver.establish(proof)
        except (ProductionOidcLoginUnavailable, ProductionIdentityUnavailable):
            response = JSONResponse(
                {"detail": "SSO authentication failed"}, status_code=401
            )
            response.delete_cookie(
                "aon_oidc_transaction", path="/auth/oidc/callback"
            )
            return response
        response = JSONResponse({"status": "authenticated"})
        response.delete_cookie("aon_oidc_transaction", path="/auth/oidc/callback")
        response.set_cookie(
            "aon_identity_session",
            current.identity_session_id,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    def require_capability() -> tuple[
        SqliteProductionRegistryUsers, ProductionPrincipalResolver
    ]:
        if not capable:
            raise HTTPException(status_code=503, detail="온보딩 capability unavailable")
        assert users is not None and principal_resolver is not None
        return users, principal_resolver

    def identity(
        session_id: str | None,
    ) -> tuple[SqliteProductionRegistryUsers, ProductionAuthenticatedIdentity]:
        store, resolver = require_capability()
        if session_id is None:
            raise HTTPException(status_code=401, detail="인증이 필요합니다.")
        try:
            return store, resolver.resolve(session_id)
        except ProductionIdentityUnavailable:
            raise HTTPException(status_code=401, detail="인증이 필요합니다.") from None

    def card_identity(
        session_id: str | None,
    ) -> tuple[SqliteProductionAgentCards, ProductionAuthenticatedIdentity]:
        if not card_capable:
            raise HTTPException(status_code=503, detail="Agent Card capability unavailable")
        _, current = identity(session_id)
        assert agent_cards is not None
        return agent_cards, current

    @app.get("/admin/users")
    def list_users(  # pyright: ignore[reportUnusedFunction]
        aon_identity_session: Annotated[str | None, Cookie()] = None,
    ) -> list[dict[str, object]]:
        store, current = identity(aon_identity_session)
        return [
            {
                "user_id": user.user_id,
                "email": user.email,
                "manager": user.manager_id,
                "sso_link_status": (
                    "verified_email_match"
                    if user.user_id == current.principal.subject_id
                    else "unlinked"
                ),
            }
            for user in store.users(current.principal.org_id)
        ]

    @app.get("/onboarding/status")
    def status(  # pyright: ignore[reportUnusedFunction]
        aon_identity_session: Annotated[str | None, Cookie()] = None,
    ) -> dict[str, object]:
        store, current = identity(aon_identity_session)
        revision = store.revision(current.principal.org_id)
        owned_cards = []
        if card_capable:
            assert agent_cards is not None
            owned_cards = [
                card
                for card in agent_cards.cards(current.principal.org_id)
                if current.principal.subject_id in {card.owner, card.maintainer}
            ]
            revision = agent_cards.revision(current.principal.org_id)
        card_complete = bool(owned_cards)
        return {
            "revision": revision,
            "card_capability": "available" if card_capable else "unavailable",
            "steps": [
                {"kind": "user", "state": "complete"},
                {
                    "kind": "card",
                    "state": (
                        "complete"
                        if card_complete
                        else "current"
                        if card_capable
                        else "locked"
                    ),
                },
                {"kind": "knowledge", "state": "current" if card_complete else "locked"},
            ],
            "cards": [card.model_dump(mode="json") for card in owned_cards],
        }

    @app.get("/admin/agent-cards")
    def list_agent_cards(  # pyright: ignore[reportUnusedFunction]
        aon_identity_session: Annotated[str | None, Cookie()] = None,
    ) -> list[dict[str, Any]]:
        store, current = card_identity(aon_identity_session)
        all_cards = store.cards(current.principal.org_id)
        can_list_all = (
            card_list_all_authorized(current)
            if card_list_all_authorized is not None
            else False
        )
        visible = (
            all_cards
            if can_list_all
            else tuple(
                card
                for card in all_cards
                if current.principal.subject_id in {card.owner, card.maintainer}
            )
        )
        return [card.model_dump(mode="json") for card in visible]

    @app.post("/admin/agent-cards")
    async def register_agent_card(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        idempotency_key: Annotated[str | None, Header()] = None,
        aon_identity_session: Annotated[str | None, Cookie()] = None,
    ) -> dict[str, object]:
        store, current = card_identity(aon_identity_session)
        if idempotency_key is None:
            raise HTTPException(status_code=422, detail="Idempotency-Key가 필요합니다.")
        try:
            raw: object = await request.json()
            if not isinstance(raw, dict):
                raise ValueError()
            body = _CardRegisterBody.model_validate(cast(dict[str, object], raw))
            if (
                body.owner != current.principal.subject_id
                and (
                    registration_delegation_authorizer is None
                    or not registration_delegation_authorizer.allows(
                        identity=current,
                        org_id=current.principal.org_id,
                        agent_id=body.agent_id,
                        owner_id=body.owner,
                    )
                )
            ):
                raise ProductionAgentCardDenied()
            card_payload = body.model_dump()
            expected_revision = card_payload.pop("expected_revision")
            command = ProductionAgentCardCommand.model_validate(
                {
                    "org_id": current.principal.org_id,
                    "principal_id": current.principal.subject_id,
                    "idempotency_key": idempotency_key,
                    "expected_revision": expected_revision,
                    "card": {
                        **card_payload,
                        "last_reviewed_at": clock().date().isoformat(),
                    },
                }
            )
            result = store.register(command)
        except ProductionAgentCardRevisionConflict:
            raise HTTPException(status_code=409, detail="Registry revision conflict") from None
        except ProductionAgentCardConflict:
            raise HTTPException(status_code=409, detail="Agent Card conflict") from None
        except ProductionAgentCardDenied:
            raise HTTPException(status_code=403, detail="권한이 없습니다.") from None
        except ProductionAgentCardUnavailable:
            raise HTTPException(status_code=503, detail="Agent Card capability unavailable") from None
        except Exception as error:
            from pydantic import ValidationError

            if isinstance(error, (KeyError, ValidationError, ValueError)):
                raise HTTPException(status_code=422, detail="등록 필드가 유효하지 않습니다.") from None
            raise
        return {
            "card": result.card.model_dump(mode="json"),
            "revision": result.revision,
            "replayed": result.replayed,
        }

    @app.post("/admin/users")
    async def register_user(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        idempotency_key: Annotated[str | None, Header()] = None,
        aon_identity_session: Annotated[str | None, Cookie()] = None,
    ) -> dict[str, object]:
        # Capability and opaque session are resolved before reading attacker-controlled body.
        store, current = identity(aon_identity_session)
        if idempotency_key is None:
            raise HTTPException(status_code=422, detail="Idempotency-Key가 필요합니다.")
        try:
            body = _RegisterBody.model_validate(await request.json())
            command = ProductionRegistryUserCommand(
                org_id=current.principal.org_id,
                principal_id=current.principal.subject_id,
                idempotency_key=idempotency_key,
                expected_revision=body.expected_revision,
                user_id=body.user_id,
                email=body.email,
                manager_id=body.manager,
            )
            result = store.register(command)
        except ProductionRegistryUserRevisionConflict:
            raise HTTPException(status_code=409, detail="Registry revision conflict") from None
        except ProductionRegistryUserConflict:
            raise HTTPException(status_code=409, detail="Registry User conflict") from None
        except ProductionRegistryUserDenied:
            raise HTTPException(status_code=403, detail="권한이 없습니다.") from None
        except ProductionRegistryUserUnavailable:
            raise HTTPException(status_code=503, detail="온보딩 capability unavailable") from None
        except Exception as error:
            from pydantic import ValidationError

            if isinstance(error, (ValidationError, ValueError)):
                raise HTTPException(status_code=422, detail="등록 필드가 유효하지 않습니다.") from None
            raise
        return {
            "user_id": result.user.user_id,
            "email": result.user.email,
            "manager": result.user.manager_id,
            "revision": result.revision,
            "replayed": result.replayed,
        }

    return app


__all__ = ["CardRegistrationDelegationAuthorizer", "create_production_onboarding_app"]
