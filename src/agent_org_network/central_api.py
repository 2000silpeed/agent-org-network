"""Exact Central browser API for lifecycle, operational evidence, and policy control."""

from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from collections.abc import AsyncIterator, Mapping
from time import monotonic
from typing import Iterator, Literal, Protocol, TypedDict, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import TypeAdapter, ValidationError

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.central_composition import (
    CentralComposition,
    validate_central_installation_config,
)
from agent_org_network.central_browser_oidc import (
    BROWSER_OIDC_TRANSACTION_TTL,
    BROWSER_SESSION_TTL,
    BrowserOidcCallbackInvalid,
    BrowserOidcForbidden,
    BrowserOidcNotAdmitted,
    BrowserOidcUnauthenticated,
    BrowserOidcUnavailable,
    BrowserSessionCsrfForbidden,
    BrowserSessionForbidden,
    BrowserSessionUnauthenticated,
)
from agent_org_network.central_browser_auth import (
    constant_time_digest_matches,
    opaque_browser_handle_digest,
)
from agent_org_network.central_browser_auth_sqlite import BrowserSessionCurrentOutcome
from agent_org_network.central_question_lifecycle import (
    AnsweredProjection,
    CentralQuestionLifecycleConflict,
    CentralQuestionLifecycleUnavailable,
    FeedbackCommand,
    QuestionFeedbackConflict,
    QuestionFeedbackInvalid,
    QuestionFeedbackNotFound,
    QuestionFeedbackResult,
    QuestionCreateCommand,
    QuestionCreateForbidden,
)
from agent_org_network.question_request import (
    AnsweredRequest,
    AwaitingAnswer,
    AwaitingApproval,
    AwaitingConflict,
    DeclinedRequest,
    FailedRequest,
    QuestionRequest,
    ReadyToDispatch,
    Received,
)
from agent_org_network.central_registry_admission import (
    SessionDerivedRegistryRegistrationDenied,
    SessionDerivedRegistryRegistrationFactory,
    SessionDerivedRegistryRegistrationUnauthenticated,
    SessionDerivedRegistryRegistrationUnavailable,
)
from agent_org_network.agent_card import AgentCard
from agent_org_network.sqlite_production_agent_cards import (
    ProductionAgentCardCommand,
    ProductionAgentCardConflict,
    ProductionAgentCardDenied,
    ProductionAgentCardInvalid,
    ProductionAgentCardRevisionConflict,
    ProductionAgentCardUnavailable,
)
from agent_org_network.sqlite_production_registry_users import (
    ProductionRegistryUserCommand,
    ProductionRegistryUserConflict,
    ProductionRegistryUserDenied,
    ProductionRegistryUserRevisionConflict,
    ProductionRegistryUserUnavailable,
)
from agent_org_network.central_inbox_api import create_central_inbox_router
from agent_org_network.central_operational_evidence import (
    OperationalEvent,
    OperationalEvidenceReader,
    OperationalEvidenceResyncRequired,
)
from agent_org_network.central_policy_revision import (
    PolicyCommand,
    PolicyRevisionConflict,
    PolicyRevisionUnavailable,
)


_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_STREAM_CURSOR = re.compile(r"[1-9][0-9]{0,18}\Z")
_OPERATIONAL_CURSOR = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")
_AUDIT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_ADMIN_CARD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_ADMIN_REASON_CODE = re.compile(r"[a-z0-9_]{1,64}\Z")
_ADMIN_RFC3339_UTC = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z"
)
_MAX_CURSOR = 9_223_372_036_854_775_807
_MAX_BODY = 64 * 1024
_CENTRAL_ADMIN_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_CENTRAL_ADMIN_REASON = re.compile(r"^[a-z0-9_]{1,64}$")
_POLICY_COMMAND_ADAPTER: TypeAdapter[PolicyCommand] = TypeAdapter(PolicyCommand)
_BROWSER_SELF_CLAIM_HEADERS = frozenset(
    {
        "authorization", "forwarded", "x-forwarded-for", "x-forwarded-host",
        "x-forwarded-proto", "x-aon-user", "x-aon-org", "x-aon-role",
        "x-aon-permission", "x-aon-token-claim", "x-aon-session",
        "x-aon-actor", "x-aon-authority",
    }
)


def _has_browser_self_claim(headers: Mapping[str, str]) -> bool:
    try:
        names = frozenset(headers.keys())
        return any(name in _BROWSER_SELF_CLAIM_HEADERS or name.startswith("x-forwarded-") for name in names)
    except Exception:
        return True


class _RegistryUserBody(TypedDict):
    expected_revision: int
    user_id: str
    email: str
    manager: str | None


class _RegistryCardBody(TypedDict):
    expected_revision: int
    agent_id: str
    owner: str
    team: str
    summary: str
    domains: list[str]
    maintainer: str | None
    can_answer: list[str]
    cannot_answer: list[str]
    approval_when: list[str]
    collaborate_when: list[str]
    knowledge_sources: list[str]
    trust_labels: list[str]


class _FeedbackBody(TypedDict):
    record_id: str
    verdict: Literal["good", "bad"]
    comment: str


class _OwnerTransferBody(TypedDict):
    new_owner_user_id: str
    expected_card_revision: int
    expected_assignment_generation: int
    expected_assignment_revision: int


class _OwnerRevokeBody(TypedDict):
    reason_code: str
    expected_card_revision: int
    expected_assignment_generation: int
    expected_assignment_revision: int


class CentralApiRunner(Protocol):
    def __call__(self, app: FastAPI, *, host: str, port: int) -> None: ...


class _OperationalProjectorRuntime:
    """Own one durable projector for exactly one Central server lifespan."""

    def __init__(self, composition: CentralComposition, *, poll_seconds: float) -> None:
        self._projector = composition.operational_evidence_projector
        self._poll_seconds = poll_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._ready = self._projector is not None

    @property
    def ready(self) -> bool:
        return self._ready

    async def start(self) -> None:
        projector = self._projector
        if projector is None:
            self._ready = False
            return
        try:
            # Startup recovery drains every pending/expired durable intent
            # before readiness can be observed by a serving request.
            await asyncio.to_thread(projector.drain)
        except Exception:
            self._ready = False
            return
        self._task = asyncio.create_task(self._run(), name="central-operational-projector")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    def mark_unavailable(self) -> None:
        self._ready = False

    async def _run(self) -> None:
        projector = self._projector
        assert projector is not None
        try:
            while not self._stop.is_set():
                try:
                    projected = await asyncio.to_thread(projector.project_one)
                except Exception:
                    self._ready = False
                    return
                if projected:
                    # Drain a backlog without a wall-clock delay, while still
                    # yielding control between individual durable UoWs.
                    await asyncio.sleep(0)
                    continue
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise


def create_central_api_app(
    composition: CentralComposition,
    *,
    operational_poll_seconds: float = 0.1,
    sse_poll_seconds: float = 0.25,
    sse_keepalive_seconds: float = 15.0,
) -> FastAPI:
    if type(composition) is not CentralComposition:
        raise TypeError("CentralComposition required")
    validate_central_installation_config(composition.config)
    for value, lower, upper in (
        (operational_poll_seconds, 0.01, 5.0),
        (sse_poll_seconds, 0.01, 5.0),
        (sse_keepalive_seconds, 0.05, 60.0),
    ):
        if type(value) not in {int, float} or not math.isfinite(float(value)) or not lower <= float(value) <= upper:
            raise ValueError("bounded Central runtime interval required")
    runtime = _OperationalProjectorRuntime(
        composition, poll_seconds=float(operational_poll_seconds)
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            recovery = composition.lifecycle_recovery
            if recovery is not None:
                recovery.recover_pending()
            await runtime.start()
            yield
        finally:
            await runtime.stop()
            composition.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.operational_runtime = runtime
    app.state.sse_poll_seconds = float(sse_poll_seconds)
    app.state.sse_keepalive_seconds = float(sse_keepalive_seconds)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if composition.intake_ready() and runtime.ready:
            return JSONResponse({"status": "ready"})
        return _error(503, "central_intake_unavailable")

    @app.post("/v1/browser-auth/login/start")
    async def browser_login_start(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> Response:
        if not await _strict_browser_start_request(request, composition.config.central_public_origin):
            return _error(403, "browser_origin_forbidden")
        browser_oidc = composition.browser_oidc
        if browser_oidc is None:
            return _error(503, "browser_session_unavailable")
        try:
            wire = browser_oidc.begin()
            response = RedirectResponse(wire.authorization_url, status_code=303)
            response.set_cookie(
                "__Host-aon-central-oidc-tx", wire.transaction_handle,
                max_age=int(BROWSER_OIDC_TRANSACTION_TTL.total_seconds()), secure=True, httponly=True, samesite="lax", path="/",
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        except BrowserOidcUnavailable:
            return _error(503, "browser_session_unavailable")
        except Exception:
            return _error(503, "browser_session_unavailable")

    @app.get("/v1/browser-auth/callback")
    def browser_login_callback(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> Response:
        browser_oidc = composition.browser_oidc
        values = tuple(request.query_params.multi_items())
        code, state, standard_error = _strict_callback_values(values)
        transaction_handle = request.cookies.get("__Host-aon-central-oidc-tx")
        if browser_oidc is None:
            return _expired_transaction_error(503, "browser_session_unavailable")
        if standard_error:
            if type(state) is not str or type(transaction_handle) is not str:
                return _expired_transaction_error(400, "browser_oidc_callback_invalid")
            try:
                browser_oidc.cancel(transaction_handle=transaction_handle, state=state)
                return _expired_transaction_error(401, "browser_oidc_unauthenticated")
            except BrowserOidcCallbackInvalid:
                return _expired_transaction_error(400, "browser_oidc_callback_invalid")
            except BrowserOidcUnavailable:
                return _expired_transaction_error(503, "browser_session_unavailable")
        if (
            type(code) is not str
            or type(state) is not str
            or type(transaction_handle) is not str
        ):
            return _expired_transaction_error(400, "browser_oidc_callback_invalid")
        try:
            wire = browser_oidc.complete(
                transaction_handle=transaction_handle, state=state, authorization_code=code
            )
            response = RedirectResponse("/ask", status_code=303)
            response.delete_cookie("__Host-aon-central-oidc-tx", secure=True, httponly=True, samesite="lax", path="/")
            response.set_cookie(
                "__Host-aon-central-session", wire.session_handle,
                max_age=int(BROWSER_SESSION_TTL.total_seconds()), secure=True, httponly=True, samesite="lax", path="/",
            )
            response.set_cookie(
                "__Host-aon-central-csrf", wire.csrf_token,
                max_age=int(BROWSER_SESSION_TTL.total_seconds()), secure=True, httponly=False, samesite="strict", path="/",
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        except BrowserOidcCallbackInvalid:
            return _expired_transaction_error(400, "browser_oidc_callback_invalid")
        except BrowserOidcUnauthenticated:
            return _expired_transaction_error(401, "browser_oidc_unauthenticated")
        except BrowserOidcNotAdmitted:
            return _expired_transaction_error(403, "registry_user_not_admitted")
        except BrowserOidcForbidden:
            return _expired_transaction_error(403, "browser_session_forbidden")
        except BrowserOidcUnavailable:
            return _expired_transaction_error(503, "browser_session_unavailable")
        except Exception:
            return _expired_transaction_error(503, "browser_session_unavailable")

    @app.get("/v1/browser-auth/session")
    async def browser_session_current(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> JSONResponse:
        if not await _strict_browser_session_request(request):
            return _browser_session_error(403, "browser_session_forbidden")
        browser_sessions = composition.browser_sessions
        session_handle = request.cookies.get("__Host-aon-central-session")
        if browser_sessions is None:
            return _browser_session_error(503, "browser_session_unavailable")
        if type(session_handle) is not str:
            return _browser_session_error(401, "browser_session_unauthenticated")
        try:
            projection = browser_sessions.read(session_handle=session_handle)
            return _browser_session_projection(projection)
        except BrowserSessionUnauthenticated:
            return _browser_session_error(401, "browser_session_unauthenticated")
        except BrowserSessionForbidden:
            return _browser_session_error(403, "browser_session_forbidden")
        except BrowserOidcUnavailable:
            return _browser_session_error(503, "browser_session_unavailable")
        except Exception:
            return _browser_session_error(503, "browser_session_unavailable")

    @app.post("/v1/browser-auth/logout", status_code=204)
    async def browser_session_logout(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> Response:
        if not await _strict_browser_logout_request(request, composition.config.central_public_origin):
            return _browser_session_error(403, "browser_csrf_forbidden")
        browser_sessions = composition.browser_sessions
        session_handle = request.cookies.get("__Host-aon-central-session")
        csrf_cookie = request.cookies.get("__Host-aon-central-csrf")
        csrf_header = request.headers.get("x-aon-csrf")
        if browser_sessions is None:
            return _browser_session_error(503, "browser_session_unavailable")
        try:
            browser_sessions.end(
                session_handle=session_handle if type(session_handle) is str else "",
                csrf_cookie=csrf_cookie if type(csrf_cookie) is str else "",
                csrf_header=csrf_header if type(csrf_header) is str else "",
            )
            return _expired_browser_session_response()
        except BrowserSessionCsrfForbidden:
            return _browser_session_error(403, "browser_csrf_forbidden")
        except BrowserOidcUnavailable:
            return _browser_session_error(503, "browser_session_unavailable")
        except Exception:
            return _browser_session_error(503, "browser_session_unavailable")

    @app.get("/admin/users")
    async def list_registry_users(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> JSONResponse:
        if not await _strict_registry_get_request(request):
            return _registry_error(403, "browser_session_forbidden")
        resolved = _registry_admission_factory(composition, request)
        if isinstance(resolved, JSONResponse):
            return resolved
        factory, _session_digest = resolved
        try:
            principal, _revision, users = factory.read_user_projection(action="user.register")
            return JSONResponse(
                [
                    {
                        "user_id": user.user_id,
                        "email": user.email,
                        "manager": user.manager_id,
                        "sso_link_status": (
                            "verified_email_match"
                            if user.user_id == principal.registry_user_id
                            else "unlinked"
                        ),
                    }
                    for user in users
                ],
                headers={"Cache-Control": "no-store"},
            )
        except SessionDerivedRegistryRegistrationDenied:
            return _registry_error(403, "registry_registration_forbidden")
        except SessionDerivedRegistryRegistrationUnauthenticated:
            return _registry_error(401, "browser_session_unauthenticated")
        except SessionDerivedRegistryRegistrationUnavailable:
            return _registry_error(503, "registry_registration_unavailable")
        except Exception:
            return _registry_error(503, "registry_registration_unavailable")

    @app.post("/admin/users")
    async def register_registry_user(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> JSONResponse:
        if not await _strict_registry_post_envelope(request, composition.config.central_public_origin):
            return _registry_error(403, "browser_csrf_forbidden")
        resolved = _registry_admission_factory(composition, request)
        if isinstance(resolved, JSONResponse):
            return resolved
        factory, session_digest = resolved
        csrf_cookie = request.cookies.get("__Host-aon-central-csrf")
        csrf_header = request.headers.get("x-aon-csrf")
        browser_auth = composition.browser_auth
        if browser_auth is None:
            return _registry_error(503, "registry_registration_unavailable")
        try:
            session = browser_auth.get_session(session_digest)
            if (
                session is None
                or type(csrf_cookie) is not str
                or type(csrf_header) is not str
                or not csrf_cookie
                or not constant_time_digest_matches(csrf_cookie, session.csrf_digest)
                or not constant_time_digest_matches(csrf_header, session.csrf_digest)
            ):
                return _registry_error(403, "browser_csrf_forbidden")
        except Exception:
            return _registry_error(503, "registry_registration_unavailable")
        key = request.headers.get("idempotency-key")
        if type(key) is not str or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            return _registry_error(422, "invalid_registration_request")
        try:
            # This pre-body action check prevents a rejected/expired session or
            # revoked user.register grant from becoming a body parser oracle.
            # The mutable store repeats this current read in its own UoW and at
            # precommit; this value is only the derived principal for command
            # construction.
            principal = factory.read_current(action="user.register")
        except SessionDerivedRegistryRegistrationDenied:
            return _registry_error(403, "registry_registration_forbidden")
        except SessionDerivedRegistryRegistrationUnauthenticated:
            return _registry_error(401, "browser_session_unauthenticated")
        except SessionDerivedRegistryRegistrationUnavailable:
            return _registry_error(503, "registry_registration_unavailable")
        body = await _registry_user_body(request)
        if body is None:
            return _registry_error(422, "invalid_registration_request")
        try:
            # Factory current()/precommit re-read the durable session, Registry
            # binding and user.register Authority in the write transaction.
            application = factory.create()
            try:
                command = ProductionRegistryUserCommand(
                    org_id=composition.config.org_id,
                    principal_id=principal.registry_user_id,
                    idempotency_key=key,
                    expected_revision=body["expected_revision"],
                    user_id=body["user_id"],
                    email=body["email"],
                    manager_id=body["manager"],
                )
                result = application.users.register(command)
            finally:
                application.close()
            return JSONResponse(
                {
                    "user_id": result.user.user_id,
                    "email": result.user.email,
                    "manager": result.user.manager_id,
                    "revision": result.revision,
                    "replayed": result.replayed,
                },
                headers={"Cache-Control": "no-store"},
            )
        except SessionDerivedRegistryRegistrationUnauthenticated:
            return _registry_error(401, "browser_session_unauthenticated")
        except (SessionDerivedRegistryRegistrationDenied, ProductionRegistryUserDenied):
            return _registry_error(403, "registry_registration_forbidden")
        except (SessionDerivedRegistryRegistrationUnavailable, ProductionRegistryUserUnavailable):
            return _registry_error(503, "registry_registration_unavailable")
        except ProductionRegistryUserRevisionConflict:
            return _registry_error(409, "registry_revision_conflict")
        except ProductionRegistryUserConflict:
            return _registry_error(409, "registry_registration_conflict")
        except (TypeError, ValueError):
            return _registry_error(422, "invalid_registration_request")
        except Exception:
            return _registry_error(503, "registry_registration_unavailable")

    @app.get("/admin/agent-cards")
    async def list_agent_cards(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> JSONResponse:
        if not await _strict_registry_get_request(request):
            return _registry_error(403, "browser_session_forbidden")
        resolved = _registry_admission_factory(composition, request)
        if isinstance(resolved, JSONResponse):
            return resolved
        factory, _session_digest = resolved
        try:
            _principal, _revision, cards = factory.read_card_projection(action="card.register")
            return JSONResponse(
                [card.model_dump(mode="json") for card in cards],
                headers={"Cache-Control": "no-store"},
            )
        except SessionDerivedRegistryRegistrationDenied:
            return _registry_error(403, "registry_registration_forbidden")
        except SessionDerivedRegistryRegistrationUnauthenticated:
            return _registry_error(401, "browser_session_unauthenticated")
        except SessionDerivedRegistryRegistrationUnavailable:
            return _registry_error(503, "registry_registration_unavailable")
        except Exception:
            return _registry_error(503, "registry_registration_unavailable")

    @app.post("/admin/agent-cards")
    async def register_agent_card(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> JSONResponse:
        if not await _strict_registry_post_envelope(request, composition.config.central_public_origin):
            return _registry_error(403, "browser_csrf_forbidden")
        resolved = _registry_admission_factory(composition, request)
        if isinstance(resolved, JSONResponse):
            return resolved
        factory, session_digest = resolved
        csrf_cookie = request.cookies.get("__Host-aon-central-csrf")
        csrf_header = request.headers.get("x-aon-csrf")
        browser_auth = composition.browser_auth
        if browser_auth is None:
            return _registry_error(503, "registry_registration_unavailable")
        try:
            session = browser_auth.get_session(session_digest)
            if (
                session is None
                or type(csrf_cookie) is not str
                or type(csrf_header) is not str
                or not csrf_cookie
                or not constant_time_digest_matches(csrf_cookie, session.csrf_digest)
                or not constant_time_digest_matches(csrf_header, session.csrf_digest)
            ):
                return _registry_error(403, "browser_csrf_forbidden")
        except Exception:
            return _registry_error(503, "registry_registration_unavailable")
        key = request.headers.get("idempotency-key")
        if type(key) is not str or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            return _registry_error(422, "invalid_registration_request")
        try:
            principal = factory.read_current(action="card.register")
        except SessionDerivedRegistryRegistrationDenied:
            return _registry_error(403, "registry_registration_forbidden")
        except SessionDerivedRegistryRegistrationUnauthenticated:
            return _registry_error(401, "browser_session_unauthenticated")
        except SessionDerivedRegistryRegistrationUnavailable:
            return _registry_error(503, "registry_registration_unavailable")
        body = await _registry_card_body(request)
        if body is None:
            return _registry_error(422, "invalid_registration_request")
        browser_clock = composition.browser_clock
        if browser_clock is None:
            return _registry_error(503, "registry_registration_unavailable")
        try:
            reviewed_at = browser_clock()
            if reviewed_at.tzinfo is None:
                return _registry_error(503, "registry_registration_unavailable")
            card = AgentCard.model_validate({
                key: value for key, value in body.items() if key != "expected_revision"
            } | {
                "last_reviewed_at": reviewed_at.date().isoformat(),
            })
            application = factory.create()
            try:
                result = application.cards.register(
                    ProductionAgentCardCommand(
                        org_id=composition.config.org_id,
                        principal_id=principal.registry_user_id,
                        idempotency_key=key,
                        expected_revision=body["expected_revision"],
                        card=card,
                    )
                )
            finally:
                application.close()
            return JSONResponse(
                {
                    "card": result.card.model_dump(mode="json"),
                    "revision": result.revision,
                    "replayed": result.replayed,
                },
                headers={"Cache-Control": "no-store"},
            )
        except SessionDerivedRegistryRegistrationUnauthenticated:
            return _registry_error(401, "browser_session_unauthenticated")
        except (SessionDerivedRegistryRegistrationDenied, ProductionAgentCardDenied):
            return _registry_error(403, "registry_registration_forbidden")
        except (SessionDerivedRegistryRegistrationUnavailable, ProductionAgentCardUnavailable):
            return _registry_error(503, "registry_registration_unavailable")
        except ProductionAgentCardRevisionConflict:
            return _registry_error(409, "registry_revision_conflict")
        except (ProductionAgentCardInvalid, TypeError, ValueError):
            return _registry_error(422, "invalid_registration_request")
        except ProductionAgentCardConflict:
            return _registry_error(409, "registry_registration_conflict")
        except Exception:
            return _registry_error(503, "registry_registration_unavailable")

    @app.get("/onboarding/status")
    async def onboarding_status(  # pyright: ignore[reportUnusedFunction]
        request: Request,
    ) -> JSONResponse:
        if not await _strict_registry_get_request(request):
            return _registry_error(403, "browser_session_forbidden")
        resolved = _registry_admission_factory(composition, request)
        if isinstance(resolved, JSONResponse):
            return resolved
        factory, _session_digest = resolved
        try:
            principal, revision, cards = factory.read_card_projection(action="session.read")
            owned = tuple(card for card in cards if card.owner == principal.registry_user_id)
            card_state = "complete" if owned else "current"
            installation_state = "current" if owned else "locked"
            return JSONResponse(
                {
                    "revision": revision,
                    "card_capability": "available",
                    "cards": [
                        {
                            "agent_id": card.agent_id,
                            "owner": card.owner,
                            "team": card.team,
                            "summary": card.summary,
                        }
                        for card in owned
                    ],
                    "card_owner_installation": {
                        "artifact": "agent-org-owner",
                        "href": "/onboarding#card-owner-installation",
                    },
                    "steps": [
                        {"kind": "user", "label": "Registry User", "state": "complete"},
                        {"kind": "card", "label": "Agent Card", "state": card_state},
                        {
                            "kind": "card_owner_installation",
                            "label": "Card Owner Installation",
                            "state": installation_state,
                        },
                    ],
                },
                headers={"Cache-Control": "no-store"},
            )
        except SessionDerivedRegistryRegistrationDenied:
            return _registry_error(403, "browser_session_forbidden")
        except SessionDerivedRegistryRegistrationUnauthenticated:
            return _registry_error(401, "browser_session_unauthenticated")
        except SessionDerivedRegistryRegistrationUnavailable:
            return _registry_error(503, "registry_registration_unavailable")
        except Exception:
            return _registry_error(503, "registry_registration_unavailable")

    @app.post("/v1/questions", status_code=201)
    async def create_question(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        principal_or_error = _browser_question_principal(composition, request, "question.create")
        if isinstance(principal_or_error, JSONResponse):
            return principal_or_error
        if not await _strict_question_post_envelope(request, composition.config.central_public_origin):
            return _error(403, "browser_csrf_forbidden")
        if not _csrf_matches(composition, request, principal_or_error):
            return _error(403, "browser_csrf_forbidden")
        key = request.headers.get("idempotency-key")
        question = await _question_body(request)
        if _IDEMPOTENCY_KEY.fullmatch(key or "") is None or question is None:
            return _error(422, "invalid_question_request")
        assert key is not None
        creator = composition.question_create
        recovery = composition.lifecycle_recovery
        if creator is None or recovery is None:
            return _error(503, "question_lifecycle_unavailable")
        try:
            result = creator.create(QuestionCreateCommand(
                question=question, idempotency_key=key,
                identity_session_id=principal_or_error.identity_session_id,
                expected_org_id=principal_or_error.org_id,
                expected_requester_id=principal_or_error.subject_id,
            ))
            # Receipt first: recovery runs strictly after the durable Received
            # commit and cannot alter this original HTTP receipt.
            if not result.replayed:
                recovery.recover_one(result.request.request_id)
            return JSONResponse(_received_wire(result.request, result.replayed), status_code=201)
        except CentralQuestionLifecycleConflict:
            return _error(409, "question_request_conflict")
        except QuestionCreateForbidden:
            return _error(403, "question_forbidden")
        except CentralQuestionLifecycleUnavailable:
            return _error(503, "question_lifecycle_unavailable")
        except Exception:
            return _error(503, "question_lifecycle_unavailable")

    @app.get("/v1/questions/{request_id}/stream")
    async def stream_question(request: Request, request_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not await _strict_question_stream_envelope(request, request_id):
            return _error(422, "invalid_question_stream_request")
        principal_or_error = _browser_question_principal(composition, request, "question.read", request_id)
        if isinstance(principal_or_error, JSONResponse):
            return principal_or_error
        projection = _owned_lifecycle_projection(composition, request_id, principal_or_error)
        if isinstance(projection, JSONResponse):
            return projection
        cursor = request.headers.get("last-event-id")
        # A fresh connection announces durable acceptance then its canonical
        # state.  Reconnects deliberately omit volatile token replay.  The
        # rechecks are intentional: an already-open stream must never emit a
        # lifecycle payload after session/Authority revocation.
        def frames() -> Iterator[str]:
            event_id = 1
            renewed = _browser_question_principal(composition, request, "question.read", request_id)
            if isinstance(renewed, JSONResponse):
                retryable = renewed.status_code == 503
                yield _sse_frame(event_id, "interrupted", {"request_id": request_id, "retryable": retryable})
                return
            if cursor is None:
                yield _sse_frame(event_id, "accepted", {"request_id": request_id})
                event_id += 1
            renewed = _browser_question_principal(composition, request, "question.read", request_id)
            if isinstance(renewed, JSONResponse):
                retryable = renewed.status_code == 503
                yield _sse_frame(event_id, "interrupted", {"request_id": request_id, "retryable": retryable})
                return
            current = _owned_lifecycle_projection(composition, request_id, renewed)
            if isinstance(current, JSONResponse):
                retryable = current.status_code == 503
                yield _sse_frame(event_id, "interrupted", {"request_id": request_id, "retryable": retryable})
                return
            event = "done" if current.get("type") == "answered" else str(current["type"])
            if event not in {"pending", "done", "declined", "failed"}:
                yield _sse_frame(event_id, "interrupted", {"request_id": request_id, "retryable": True})
                return
            yield _sse_frame(event_id, event, current)
        return StreamingResponse(frames(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform"})

    @app.get("/v1/questions/{request_id}")
    async def get_question(request: Request, request_id: str) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if not await _strict_question_get_envelope(request, request_id):
            return _error(422, "invalid_question_request")
        principal_or_error = _browser_question_principal(composition, request, "question.read", request_id)
        if isinstance(principal_or_error, JSONResponse):
            return principal_or_error
        projection = _owned_lifecycle_projection(composition, request_id, principal_or_error)
        return projection if isinstance(projection, JSONResponse) else JSONResponse(projection)

    @app.post("/v1/questions/{request_id}/feedback", status_code=201)
    async def submit_question_feedback(request: Request, request_id: str) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if not _valid_request_path(request, request_id):
            return _error(422, "invalid_question_feedback")
        principal_or_error = _browser_question_principal(composition, request, "feedback.create", request_id)
        if isinstance(principal_or_error, JSONResponse):
            return principal_or_error
        if not await _strict_question_post_envelope(request, composition.config.central_public_origin):
            return _error(403, "browser_csrf_forbidden")
        if not _csrf_matches(composition, request, principal_or_error):
            return _error(403, "browser_csrf_forbidden")
        key = request.headers.get("idempotency-key")
        body = await _feedback_body(request)
        if _IDEMPOTENCY_KEY.fullmatch(key or "") is None or body is None:
            return _error(422, "invalid_question_feedback")
        assert key is not None
        authority = composition.question_feedback_authority
        store = composition.lifecycle_store
        if authority is None or store is None:
            return _error(503, "question_lifecycle_unavailable")
        try:
            result = store.submit_feedback(
                FeedbackCommand(request_id=request_id, record_id=body["record_id"], principal=principal_or_error,
                    verdict=body["verdict"], comment=body["comment"], idempotency_key=key),
                authority=authority, feedback_id_factory=lambda: __import__("uuid").uuid4().hex,
                clock=lambda: _browser_now(composition),
            )
            return JSONResponse(_feedback_wire(result), status_code=201)
        except QuestionFeedbackConflict:
            return _error(409, "question_feedback_conflict")
        except (QuestionFeedbackInvalid,):
            return _error(422, "invalid_question_feedback")
        except QuestionFeedbackNotFound:
            return _error(404, "question_not_found")
        except CentralQuestionLifecycleUnavailable:
            return _error(503, "question_lifecycle_unavailable")
        except Exception:
            return _error(503, "question_lifecycle_unavailable")

    @app.get("/v1/console/feed")
    async def console_feed(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        """Return only durable, redacted operational evidence as an SSE cursor stream."""
        last_event_id = await _operational_feed_cursor(request)
        if isinstance(last_event_id, JSONResponse):
            return last_event_id
        principal_or_error = _operational_principal(
            composition, request, action="monitor.read", resource_kind="operational_feed",
            resource_id=composition.config.org_id,
        )
        if isinstance(principal_or_error, Response):
            return principal_or_error
        reader = composition.operational_evidence
        if reader is None:
            return _operational_error(503, "operational_evidence_unavailable")
        try:
            if last_event_id is None:
                # A fresh connection starts strictly after the high-water
                # observed in this canonical durable read.
                _items, _oldest, cursor, _next = reader.audit_list(
                    principal_or_error.org_id, limit=1
                )
                events = ()
            else:
                cursor = last_event_id
                events = reader.feed(principal_or_error.org_id, cursor)
        except OperationalEvidenceResyncRequired as error:
            return StreamingResponse(
                _operational_resync_frames(
                    composition, request, error.oldest_available_cursor,
                    error.latest_cursor,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache, no-transform"},
            )
        except Exception:
            runtime.mark_unavailable()
            return _operational_error(503, "operational_evidence_unavailable")

        return StreamingResponse(
            _operational_event_frames(
                composition,
                request,
                reader=reader,
                initial_cursor=cursor,
                initial_events=events,
                runtime=runtime,
                poll_seconds=float(app.state.sse_poll_seconds),
                keepalive_seconds=float(app.state.sse_keepalive_seconds),
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform"},
        )

    @app.get("/v1/console/audit")
    async def console_audit_list(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        parsed = await _operational_audit_list_query(request)
        if isinstance(parsed, JSONResponse):
            return parsed
        before_cursor, limit = parsed
        principal_or_error = _operational_principal(
            composition, request, action="audit.read", resource_kind="audit_collection",
            resource_id=composition.config.org_id,
        )
        if isinstance(principal_or_error, Response):
            return principal_or_error
        reader = composition.operational_evidence
        if reader is None:
            return _operational_error(503, "operational_evidence_unavailable")
        try:
            items, oldest, latest, next_before = reader.audit_list(
                principal_or_error.org_id, before_cursor=before_cursor, limit=limit
            )
            # A forward/nonexistent cursor is also a non-canonical resume
            # point.  Do not quietly serve a different page.
            if before_cursor is not None and before_cursor > latest + 1:
                return _operational_resync_response(oldest, latest)
            return JSONResponse(
                {
                    "items": [item.model_dump(mode="json") for item in items],
                    "oldest_available_cursor": oldest,
                    "latest_cursor": latest,
                    "next_before_cursor": next_before,
                },
                headers={"Cache-Control": "no-store"},
            )
        except OperationalEvidenceResyncRequired as error:
            return _operational_resync_response(error.oldest_available_cursor, error.latest_cursor)
        except Exception:
            return _operational_error(503, "operational_evidence_unavailable")

    @app.get("/v1/console/audit/{audit_id}")
    async def console_audit_detail(  # pyright: ignore[reportUnusedFunction]
        request: Request, audit_id: str
    ) -> Response:
        if not await _operational_audit_detail_request(request, audit_id):
            return _operational_error(422, "invalid_operational_request")
        principal_or_error = _operational_principal(
            composition, request, action="audit.read", resource_kind="audit_record",
            resource_id=audit_id, hidden_on_action_denial=True,
        )
        if isinstance(principal_or_error, Response):
            return principal_or_error
        reader = composition.operational_evidence
        if reader is None:
            return _operational_error(503, "operational_evidence_unavailable")
        try:
            item = reader.audit_detail(principal_or_error.org_id, audit_id)
            if item is None:
                return _hidden_operational_not_found()
            return JSONResponse(item.model_dump(mode="json"), headers={"Cache-Control": "no-store"})
        except OperationalEvidenceResyncRequired:
            # Detail is never a cursor resume surface.  Hide a pruned/foreign
            # object rather than revealing retention state by identifier.
            return _hidden_operational_not_found()
        except Exception:
            return _operational_error(503, "operational_evidence_unavailable")

    @app.get("/v1/console/org")
    async def console_org(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not await _strict_empty_browser_get(request):
            return _central_admin_error(422, "invalid_central_admin_request")
        principal_or_error = _central_admin_principal(
            composition, request, action="org_graph.read", resource_kind="organization_graph",
            resource_id=composition.config.org_id,
        )
        if isinstance(principal_or_error, Response):
            return principal_or_error
        capability = _central_admin_capability(composition)
        if capability is None:
            return _central_admin_error(503, "central_admin_unavailable")
        try:
            payload = _central_admin_call(
                capability, "graph", org_id=principal_or_error.org_id,
            )
            if payload is None:
                return _central_admin_error(503, "central_admin_unavailable")
            return JSONResponse(payload, headers={"Cache-Control": "no-store"})
        except Exception:
            return _central_admin_error(503, "central_admin_unavailable")

    @app.get("/v1/admin/scorecard")
    async def admin_scorecard(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        parsed = await _central_admin_scorecard_query(request, composition)
        if isinstance(parsed, JSONResponse):
            return parsed
        since, until = parsed
        principal_or_error = _central_admin_principal(
            composition, request, action="scorecard.organization.read",
            resource_kind="organization_scorecard", resource_id=composition.config.org_id,
        )
        if isinstance(principal_or_error, Response):
            return principal_or_error
        capability = _central_admin_capability(composition)
        if capability is None:
            return _central_admin_error(503, "central_admin_unavailable")
        try:
            payload = _central_admin_call(
                capability, "scorecard", org_id=principal_or_error.org_id,
                since=since, until=until,
            )
            if payload is None:
                return _central_admin_error(503, "central_admin_unavailable")
            return JSONResponse(payload, headers={"Cache-Control": "no-store"})
        except Exception:
            return _central_admin_error(503, "central_admin_unavailable")

    @app.post("/v1/admin/agent-cards/{card_id}/owner-transfers", status_code=201)
    async def admin_owner_transfer(request: Request, card_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not _valid_admin_card_path(request, card_id, suffix="owner-transfers"):
            return _central_admin_error(422, "invalid_central_admin_request")
        if not await _strict_question_post_envelope(
            request, composition.config.central_public_origin
        ):
            return _central_admin_error(403, "browser_csrf_forbidden")
        principal_or_error = _central_admin_principal(
            composition, request, action="card.transfer_owner", resource_kind="agent_card",
            resource_id=card_id,
        )
        if isinstance(principal_or_error, Response):
            return principal_or_error
        if not _csrf_matches(composition, request, principal_or_error):
            return _central_admin_error(403, "browser_csrf_forbidden")
        key = request.headers.get("idempotency-key")
        body = await _owner_transfer_body(request)
        if type(key) is not str or _IDEMPOTENCY_KEY.fullmatch(key) is None or body is None:
            return _central_admin_error(422, "invalid_central_admin_request")
        capability = _central_admin_capability(composition)
        if capability is None:
            return _central_admin_error(503, "central_admin_unavailable")
        try:
            payload = _central_admin_call(
                capability, "transfer", org_id=principal_or_error.org_id,
                actor_user_id=principal_or_error.subject_id, card_id=card_id,
                body=body, idempotency_key=key,
            )
            if payload is None:
                return _central_admin_error(503, "central_admin_unavailable")
            return JSONResponse(payload, status_code=201, headers={"Cache-Control": "no-store"})
        except Exception:
            return _central_admin_error(503, "central_admin_unavailable")

    @app.post("/v1/admin/agent-cards/{card_id}/revocations", status_code=201)
    async def admin_owner_revoke(request: Request, card_id: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not _valid_admin_card_path(request, card_id, suffix="revocations"):
            return _central_admin_error(422, "invalid_central_admin_request")
        if not await _strict_question_post_envelope(
            request, composition.config.central_public_origin
        ):
            return _central_admin_error(403, "browser_csrf_forbidden")
        principal_or_error = _central_admin_principal(
            composition, request, action="card.revoke", resource_kind="agent_card",
            resource_id=card_id,
        )
        if isinstance(principal_or_error, Response):
            return principal_or_error
        if not _csrf_matches(composition, request, principal_or_error):
            return _central_admin_error(403, "browser_csrf_forbidden")
        key = request.headers.get("idempotency-key")
        body = await _owner_revoke_body(request)
        if type(key) is not str or _IDEMPOTENCY_KEY.fullmatch(key) is None or body is None:
            return _central_admin_error(422, "invalid_central_admin_request")
        capability = _central_admin_capability(composition)
        if capability is None:
            return _central_admin_error(503, "central_admin_unavailable")
        try:
            payload = _central_admin_call(
                capability, "revoke", org_id=principal_or_error.org_id,
                actor_user_id=principal_or_error.subject_id, card_id=card_id,
                body=body, idempotency_key=key,
            )
            if payload is None:
                return _central_admin_error(503, "central_admin_unavailable")
            return JSONResponse(payload, status_code=201, headers={"Cache-Control": "no-store"})
        except Exception:
            return _central_admin_error(503, "central_admin_unavailable")

    @app.get("/v1/admin/policy")
    async def admin_policy_read(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not await _strict_empty_browser_get(request):
            return _policy_error(422, "invalid_policy_request")
        principal_or_error = _policy_principal(composition, request, action="policy.read")
        if isinstance(principal_or_error, Response):
            return principal_or_error
        application = composition.policy_revision
        if application is None:
            return _policy_error(503, "policy_unavailable")
        try:
            return JSONResponse(
                application.active(principal_or_error.org_id).model_dump(mode="json"),
                headers={"Cache-Control": "no-store"},
            )
        except PolicyRevisionUnavailable:
            return _policy_error(503, "policy_unavailable")
        except Exception:
            return _policy_error(503, "policy_unavailable")

    @app.post("/v1/admin/policy/revisions", status_code=201)
    async def admin_policy_revision(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not await _strict_question_post_envelope(request, composition.config.central_public_origin):
            return _policy_error(403, "browser_csrf_forbidden")
        principal_or_error = _policy_principal(composition, request, action="policy.write")
        if isinstance(principal_or_error, Response):
            return principal_or_error
        if not _csrf_matches(composition, request, principal_or_error):
            return _policy_error(403, "browser_csrf_forbidden")
        key = request.headers.get("idempotency-key")
        if type(key) is not str or _IDEMPOTENCY_KEY.fullmatch(key) is None:
            return _policy_error(422, "invalid_policy_request")
        command = await _policy_command_body(request)
        if command is None:
            return _policy_error(422, "invalid_policy_request")
        application = composition.policy_revision
        if application is None:
            return _policy_error(503, "policy_unavailable")
        try:
            receipt = application.apply(
                org_id=principal_or_error.org_id,
                actor_user_id=principal_or_error.subject_id,
                command=command,
                idempotency_key=key,
            )
            return JSONResponse(
                receipt.model_dump(mode="json"),
                status_code=201,
                headers={"Cache-Control": "no-store"},
            )
        except PolicyRevisionConflict:
            return _policy_error(409, "policy_revision_conflict")
        except PolicyRevisionUnavailable:
            return _policy_error(503, "policy_unavailable")
        except Exception:
            return _policy_error(503, "policy_unavailable")

    # Keep the installation surface graph explicit.  The current FastAPI
    # include_router implementation stores an opaque nested router that route
    # graph attestation cannot enumerate.
    app.router.routes.extend(create_central_inbox_router(composition).routes)
    return app


def _browser_now(composition: CentralComposition) -> datetime:
    clock = composition.browser_clock
    now = clock() if clock is not None else datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise CentralQuestionLifecycleUnavailable()
    return now


def _browser_question_principal(
    composition: CentralComposition, request: Request, action: str, request_id: str | None = None,
) -> AuthenticatedPrincipal | JSONResponse:
    """Resolve only the opaque session cookie and reauthorize every operation."""
    handle = request.cookies.get("__Host-aon-central-session")
    store = composition.browser_auth
    if type(handle) is not str or not handle:
        return _error(401, "browser_session_unauthenticated")
    if store is None:
        return _error(503, "question_lifecycle_unavailable")
    try:
        outcome, session = store.read_current_session(opaque_browser_handle_digest(handle), now=_browser_now(composition))
        if outcome is BrowserSessionCurrentOutcome.UNAUTHENTICATED:
            return _error(401, "browser_session_unauthenticated")
        if outcome is not BrowserSessionCurrentOutcome.ACTIVE:
            return _error(503, "question_lifecycle_unavailable")
        if session is None:
            return _error(401, "browser_session_unauthenticated")
        principal = AuthenticatedPrincipal(
            org_id=session.org_id, subject_id=session.registry_user_id,
            identity_provider=composition.config.oidc_provider_id, identity_session_id=session.session_digest,
        )
        authorizer = composition.authority
        if authorizer is None:
            return _error(503, "question_lifecycle_unavailable")
        session_resource = ResourceRef(org_id=principal.org_id, kind="browser_session", resource_id=session.session_digest,
            owner_subject_id=principal.subject_id)
        if type(authorizer.authorize(principal, "session.read", session_resource)) is not AuthorizationGrant:
            return _error(403, "browser_session_forbidden")
        resource = ResourceRef(
            org_id=principal.org_id,
            kind=("question" if action == "question.create" else "question_request") if action != "feedback.create" else "question_feedback",
            resource_id=request_id or "create", owner_subject_id=principal.subject_id,
        )
        if type(authorizer.authorize(principal, action, resource)) is not AuthorizationGrant:
            return _error(403, "question_forbidden")
        return principal
    except Exception:
        return _error(503, "question_lifecycle_unavailable")


def _operational_principal(
    composition: CentralComposition,
    request: Request,
    *,
    action: Literal["monitor.read", "audit.read"],
    resource_kind: Literal["operational_feed", "audit_collection", "audit_record"],
    resource_id: str,
    hidden_on_action_denial: bool = False,
) -> AuthenticatedPrincipal | Response:
    """Resolve a browser digest to its current Registry User, then reauthorize.

    ``read_current_session`` validates the browser session against the current
    Registry User catalog and its recorded Registry fingerprint; this endpoint
    intentionally does not trust a process-cached Registry projection.
    """
    handle = request.cookies.get("__Host-aon-central-session")
    store = composition.browser_auth
    if type(handle) is not str or not handle:
        return _operational_error(401, "browser_session_unauthenticated")
    if store is None or composition.operational_evidence is None:
        return _operational_error(503, "operational_evidence_unavailable")
    try:
        outcome, session = store.read_current_session(
            opaque_browser_handle_digest(handle), now=_browser_now(composition)
        )
        if outcome is BrowserSessionCurrentOutcome.UNAUTHENTICATED or session is None:
            return _operational_error(401, "browser_session_unauthenticated")
        if outcome is not BrowserSessionCurrentOutcome.ACTIVE:
            return _operational_error(503, "operational_evidence_unavailable")
        if session.org_id != composition.config.org_id:
            return _operational_error(401, "browser_session_unauthenticated")
        principal = AuthenticatedPrincipal(
            org_id=session.org_id,
            subject_id=session.registry_user_id,
            identity_provider=composition.config.oidc_provider_id,
            identity_session_id=session.session_digest,
        )
        authorizer = composition.authority
        if authorizer is None:
            return _operational_error(503, "operational_evidence_unavailable")
        session_resource = ResourceRef(
            org_id=principal.org_id, kind="browser_session",
            resource_id=principal.identity_session_id, owner_subject_id=principal.subject_id,
        )
        if type(authorizer.authorize(principal, "session.read", session_resource)) is not AuthorizationGrant:
            return _operational_error(403, "browser_session_forbidden")
        resource = ResourceRef(
            org_id=principal.org_id, kind=resource_kind,
            resource_id=resource_id, owner_subject_id=None,
        )
        if type(authorizer.authorize(principal, action, resource)) is not AuthorizationGrant:
            return _hidden_operational_not_found() if hidden_on_action_denial else _operational_error(
                403, "operational_forbidden"
            )
        return principal
    except Exception:
        return _operational_error(503, "operational_evidence_unavailable")


async def _operational_feed_cursor(request: Request) -> int | None | JSONResponse:
    try:
        accept_values = request.headers.getlist("accept")
        cursor_values = request.headers.getlist("last-event-id")
        if (
            tuple(request.query_params.multi_items())
            or accept_values != ["text/event-stream"]
            or len(cursor_values) > 1
            or request.headers.get("content-type") is not None
            or request.headers.get("content-length") not in {None, "0"}
            or _has_browser_self_claim(request.headers)
        ):
            return _operational_error(422, "invalid_operational_request")
        raw = cursor_values[0] if cursor_values else None
        if raw is not None and (
            _OPERATIONAL_CURSOR.fullmatch(raw) is None or int(raw) > _MAX_CURSOR
        ):
            return _operational_error(422, "invalid_operational_request")
        async for chunk in request.stream():
            if chunk:
                return _operational_error(422, "invalid_operational_request")
        return None if raw is None else int(raw)
    except Exception:
        return _operational_error(422, "invalid_operational_request")


async def _operational_resync_frames(
    composition: CentralComposition,
    request: Request,
    oldest: int,
    latest: int,
) -> AsyncIterator[str]:
    if await request.is_disconnected():
        return
    current = _operational_principal(
        composition, request, action="monitor.read", resource_kind="operational_feed",
        resource_id=composition.config.org_id,
    )
    if isinstance(current, Response):
        yield _operational_interrupted_frame(current.status_code == 503)
        return
    yield _operational_resync_frame(oldest, latest)


async def _operational_event_frames(
    composition: CentralComposition,
    request: Request,
    *,
    reader: OperationalEvidenceReader,
    initial_cursor: int,
    initial_events: tuple[OperationalEvent, ...],
    runtime: _OperationalProjectorRuntime,
    poll_seconds: float,
    keepalive_seconds: float,
) -> AsyncIterator[str]:
    """Poll durable cursors until disconnect; no process-local event queue exists."""
    cursor = initial_cursor
    pending = initial_events
    last_frame_at = monotonic()
    while True:
        try:
            if await request.is_disconnected():
                return
        except Exception:
            return

        if pending:
            for event in pending:
                try:
                    if await request.is_disconnected():
                        return
                except Exception:
                    return
                current = _operational_principal(
                    composition, request, action="monitor.read", resource_kind="operational_feed",
                    resource_id=composition.config.org_id,
                )
                if isinstance(current, Response):
                    yield _operational_interrupted_frame(current.status_code == 503)
                    return
                yield _sse_frame(
                    event.cursor, "operational_event", event.model_dump(mode="json")
                )
                cursor = event.cursor
                last_frame_at = monotonic()
            pending = ()
            continue

        if not runtime.ready:
            current = _operational_principal(
                composition, request, action="monitor.read", resource_kind="operational_feed",
                resource_id=composition.config.org_id,
            )
            if not isinstance(current, Response):
                yield _operational_interrupted_frame(True)
            return

        try:
            pending = await asyncio.to_thread(reader.feed, composition.config.org_id, cursor)
        except OperationalEvidenceResyncRequired as error:
            current = _operational_principal(
                composition, request, action="monitor.read", resource_kind="operational_feed",
                resource_id=composition.config.org_id,
            )
            if isinstance(current, Response):
                yield _operational_interrupted_frame(current.status_code == 503)
            else:
                yield _operational_resync_frame(
                    error.oldest_available_cursor, error.latest_cursor
                )
            return
        except Exception:
            runtime.mark_unavailable()
            current = _operational_principal(
                composition, request, action="monitor.read", resource_kind="operational_feed",
                resource_id=composition.config.org_id,
            )
            if not isinstance(current, Response):
                yield _operational_interrupted_frame(True)
            return
        if pending:
            continue

        now = monotonic()
        if now - last_frame_at >= keepalive_seconds:
            current = _operational_principal(
                composition, request, action="monitor.read", resource_kind="operational_feed",
                resource_id=composition.config.org_id,
            )
            if isinstance(current, Response):
                yield _operational_interrupted_frame(current.status_code == 503)
                return
            # Comment frames keep intermediaries from timing out without
            # becoming part of the durable event cursor protocol.
            yield ": keepalive\n\n"
            last_frame_at = monotonic()
        await asyncio.sleep(poll_seconds)


async def _operational_audit_list_query(
    request: Request,
) -> tuple[int | None, int] | JSONResponse:
    try:
        if (
            request.headers.get("content-type") is not None
            or request.headers.get("content-length") not in {None, "0"}
            or request.headers.get("last-event-id") is not None
            or _has_browser_self_claim(request.headers)
        ):
            return _operational_error(422, "invalid_operational_request")
        pairs = tuple(request.query_params.multi_items())
        if len(pairs) != len({key for key, _value in pairs}) or any(
            key not in {"before_cursor", "limit"} for key, _value in pairs
        ):
            return _operational_error(422, "invalid_operational_request")
        values = dict(pairs)
        raw_before = values.get("before_cursor")
        raw_limit = values.get("limit", "50")
        if (
            raw_before is not None
            and (_STREAM_CURSOR.fullmatch(raw_before) is None or int(raw_before) > _MAX_CURSOR)
        ):
            return _operational_error(422, "invalid_operational_request")
        if (
            not raw_limit.isascii() or not raw_limit.isdecimal()
            or not 1 <= int(raw_limit) <= 100
        ):
            return _operational_error(422, "invalid_operational_request")
        async for chunk in request.stream():
            if chunk:
                return _operational_error(422, "invalid_operational_request")
        return (None if raw_before is None else int(raw_before), int(raw_limit))
    except Exception:
        return _operational_error(422, "invalid_operational_request")


async def _operational_audit_detail_request(request: Request, audit_id: str) -> bool:
    try:
        raw_path = request.scope.get("raw_path", b"")
        if (
            _AUDIT_ID.fullmatch(audit_id) is None
            or not isinstance(raw_path, bytes)
            or b"%2f" in raw_path.lower()
            or request.url.path != "/v1/console/audit/" + audit_id
            or tuple(request.query_params.multi_items())
            or request.headers.get("content-type") is not None
            or request.headers.get("content-length") not in {None, "0"}
            or request.headers.get("last-event-id") is not None
            or _has_browser_self_claim(request.headers)
        ):
            return False
        async for chunk in request.stream():
            if chunk:
                return False
        return True
    except Exception:
        return False


def _operational_error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status_code, headers={"Cache-Control": "no-store"})


def _hidden_operational_not_found() -> Response:
    return Response(status_code=404, headers={"Cache-Control": "no-store"})


def _operational_resync_response(oldest: int, latest: int) -> JSONResponse:
    return JSONResponse(
        {
            "code": "resync_required",
            "oldest_available_cursor": oldest,
            "latest_cursor": latest,
        },
        status_code=409,
        headers={"Cache-Control": "no-store"},
    )


def _operational_resync_frame(oldest: int, latest: int) -> str:
    return "event: resync_required\ndata: " + json.dumps(
        {
            "code": "resync_required",
            "oldest_available_cursor": oldest,
            "latest_cursor": latest,
        },
        ensure_ascii=False, separators=(",", ":"),
    ) + "\n\n"


def _operational_interrupted_frame(retryable: bool) -> str:
    return "event: interrupted\ndata: " + json.dumps(
        {"retryable": retryable}, ensure_ascii=False, separators=(",", ":")
    ) + "\n\n"


def _csrf_matches(composition: CentralComposition, request: Request, principal: AuthenticatedPrincipal) -> bool:
    try:
        handle = request.cookies.get("__Host-aon-central-session")
        csrf_cookie = request.cookies.get("__Host-aon-central-csrf")
        csrf_header = request.headers.get("x-aon-csrf")
        if not all(type(value) is str and value for value in (handle, csrf_cookie, csrf_header)):
            return False
        store = composition.browser_auth
        if store is None:
            return False
        session = store.get_session(opaque_browser_handle_digest(cast(str, handle)))
        return (
            session is not None and session.session_digest == principal.identity_session_id
            and constant_time_digest_matches(cast(str, csrf_cookie), session.csrf_digest)
            and constant_time_digest_matches(cast(str, csrf_header), session.csrf_digest)
        )
    except Exception:
        return False


async def _strict_question_post_envelope(request: Request, origin: str) -> bool:
    try:
        if tuple(request.query_params.multi_items()) or _has_browser_self_claim(request.headers):
            return False
        if request.headers.get("origin") != origin or request.headers.get("sec-fetch-site") != "same-origin":
            return False
        if request.headers.get("sec-fetch-mode") != "cors" or request.headers.get("sec-fetch-dest") != "empty":
            return False
        length = request.headers.get("content-length")
        return length is None or (length.isascii() and length.isdecimal() and int(length) <= _MAX_BODY)
    except Exception:
        return False


async def _strict_question_get_envelope(request: Request, request_id: str) -> bool:
    return _valid_request_path(request, request_id) and await _strict_empty_browser_get(request)


async def _strict_question_stream_envelope(request: Request, request_id: str) -> bool:
    if not _valid_request_path(request, request_id) or request.headers.get("accept") != "text/event-stream":
        return False
    cursor = request.headers.get("last-event-id")
    if cursor is not None and _STREAM_CURSOR.fullmatch(cursor) is None:
        return False
    return await _strict_empty_browser_get(request, allowed_headers=frozenset({"accept", "last-event-id"}))


async def _strict_empty_browser_get(request: Request, *, allowed_headers: frozenset[str] = frozenset()) -> bool:
    try:
        if tuple(request.query_params.multi_items()) or request.headers.get("content-length") not in {None, "0"}:
            return False
        if _has_browser_self_claim(request.headers):
            return False
        if request.headers.get("content-type") is not None:
            return False
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received:
                return False
        return True
    except Exception:
        return False


async def _policy_command_body(request: Request) -> PolicyCommand | None:
    try:
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            return None
        length = request.headers.get("content-length")
        if length is not None and (not length.isascii() or not length.isdecimal() or int(length) > _MAX_BODY):
            return None
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > _MAX_BODY:
                return None
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not 2 <= len(raw) <= _MAX_BODY:
            return None
        payload = json.loads(raw, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
        command: PolicyCommand = _POLICY_COMMAND_ADAPTER.validate_python(payload, strict=True)
        return command
    except (ValueError, TypeError, ValidationError):
        return None
    except Exception:
        return None


def _central_admin_capability(composition: CentralComposition) -> object | None:
    """Return only an explicitly composed D capability.

    CentralComposition deliberately has no v21 ownership field yet.  Keeping
    this lookup narrow prevents a route from manufacturing a success from
    Registry/Card registration stores or a metadata-only fallback.
    """
    for name in ("card_owner_assignment", "card_ownership"):
        try:
            capability = getattr(composition, name, None)
        except Exception:
            return None
        if capability is not None:
            return capability
    return None


def _central_admin_call(capability: object, operation: str, **kwargs: object) -> object | None:
    method = getattr(capability, operation, None)
    if not callable(method):
        return None
    value = method(**kwargs)
    model_dump: object = getattr(value, "model_dump", None)
    if callable(model_dump):
        payload: object = model_dump(mode="json")
        if isinstance(payload, dict):
            return cast(dict[object, object], payload)
        if isinstance(payload, list):
            return cast(list[object], payload)
        if isinstance(payload, tuple):
            return cast(tuple[object, ...], payload)
        return None
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): item for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        result: list[object] = []
        items = cast(tuple[object, ...], value) if isinstance(value, tuple) else tuple(cast(list[object], value))
        for item in items:
            item_dump: object = getattr(item, "model_dump", None)
            if callable(item_dump):
                dumped: object = item_dump(mode="json")
                result.append(dumped)
            elif isinstance(item, Mapping):
                item_mapping = cast(Mapping[object, object], item)
                result.append({str(key): nested for key, nested in item_mapping.items()})
            else:
                return None
        return result
    return None


def _central_admin_error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status_code, headers={"Cache-Control": "no-store"})


def _central_admin_principal(
    composition: CentralComposition,
    request: Request,
    *,
    action: Literal[
        "org_graph.read", "scorecard.organization.read", "card.transfer_owner", "card.revoke"
    ],
    resource_kind: Literal["organization_graph", "organization_scorecard", "agent_card"],
    resource_id: str,
) -> AuthenticatedPrincipal | Response:
    handle = request.cookies.get("__Host-aon-central-session")
    store = composition.browser_auth
    authorizer = composition.authority
    if type(handle) is not str or not handle:
        return _central_admin_error(401, "browser_session_unauthenticated")
    if store is None or authorizer is None:
        return _central_admin_error(503, "central_admin_unavailable")
    try:
        outcome, session = store.read_current_session(
            opaque_browser_handle_digest(handle), now=_browser_now(composition)
        )
        if outcome is BrowserSessionCurrentOutcome.UNAUTHENTICATED or session is None:
            return _central_admin_error(401, "browser_session_unauthenticated")
        if outcome is not BrowserSessionCurrentOutcome.ACTIVE:
            return _central_admin_error(503, "central_admin_unavailable")
        if session.org_id != composition.config.org_id:
            return _central_admin_error(401, "browser_session_unauthenticated")
        principal = AuthenticatedPrincipal(
            org_id=session.org_id,
            subject_id=session.registry_user_id,
            identity_provider=composition.config.oidc_provider_id,
            identity_session_id=session.session_digest,
        )
        session_resource = ResourceRef(
            org_id=principal.org_id, kind="browser_session",
            resource_id=principal.identity_session_id, owner_subject_id=principal.subject_id,
        )
        if type(authorizer.authorize(principal, "session.read", session_resource)) is not AuthorizationGrant:
            return _central_admin_error(403, "browser_session_forbidden")
        owner_subject_id: str | None = None
        if resource_kind == "agent_card":
            try:
                with sqlite3.connect(
                    f"file:{composition.config.database_path}?mode=ro", uri=True
                ) as connection:
                    row = connection.execute(
                        "SELECT owner_id FROM production_agent_cards WHERE org_id=? AND agent_id=?",
                        (principal.org_id, resource_id),
                    ).fetchone()
                if row is None:
                    return _central_admin_error(404, "card_not_found")
                owner_subject_id = str(row[0])
            except Exception:
                return _central_admin_error(503, "central_admin_unavailable")
        resource = ResourceRef(
            org_id=principal.org_id, kind=resource_kind, resource_id=resource_id,
            owner_subject_id=owner_subject_id,
        )
        if type(authorizer.authorize(principal, action, resource)) is not AuthorizationGrant:
            return _central_admin_error(403, "central_admin_forbidden")
        return principal
    except Exception:
        return _central_admin_error(503, "central_admin_unavailable")


async def _central_admin_scorecard_query(
    request: Request, composition: CentralComposition,
) -> tuple[datetime | None, datetime | None] | JSONResponse:
    try:
        if (
            request.headers.get("content-type") is not None
            or request.headers.get("content-length") not in {None, "0"}
            or request.headers.get("last-event-id") is not None
            or _has_browser_self_claim(request.headers)
        ):
            return _central_admin_error(422, "invalid_central_admin_request")
        pairs = tuple(request.query_params.multi_items())
        if len(pairs) != len({key for key, _value in pairs}) or any(
            key not in {"since", "until"} for key, _value in pairs
        ):
            return _central_admin_error(422, "invalid_central_admin_request")
        values = dict(pairs)
        raw_since, raw_until = values.get("since"), values.get("until")
        if (raw_since is None) != (raw_until is None):
            return _central_admin_error(422, "invalid_central_admin_request")
        if raw_since is None or raw_until is None:
            since = until = None
        else:
            if _ADMIN_RFC3339_UTC.fullmatch(raw_since) is None or _ADMIN_RFC3339_UTC.fullmatch(raw_until) is None:
                return _central_admin_error(422, "invalid_central_admin_request")
            since = datetime.fromisoformat(raw_since[:-1] + "+00:00")
            until = datetime.fromisoformat(raw_until[:-1] + "+00:00")
            if since > until:
                return _central_admin_error(422, "invalid_central_admin_request")
        async for chunk in request.stream():
            if chunk:
                return _central_admin_error(422, "invalid_central_admin_request")
        _ = composition
        return since, until
    except Exception:
        return _central_admin_error(422, "invalid_central_admin_request")


def _valid_admin_card_path(request: Request, card_id: str, *, suffix: str) -> bool:
    raw_path = request.scope.get("raw_path", b"")
    return (
        _ADMIN_CARD_ID.fullmatch(card_id) is not None
        and isinstance(raw_path, bytes)
        and b"%2f" not in raw_path.lower()
        and request.url.path == f"/v1/admin/agent-cards/{card_id}/{suffix}"
    )


async def _owner_transfer_body(request: Request) -> _OwnerTransferBody | None:
    payload = await _exact_json_body(request)
    if payload is None or set(payload) != {
        "new_owner_user_id", "expected_card_revision", "expected_assignment_generation",
        "expected_assignment_revision",
    }:
        return None
    owner = payload["new_owner_user_id"]
    values = tuple(payload[key] for key in (
        "expected_card_revision", "expected_assignment_generation", "expected_assignment_revision",
    ))
    if type(owner) is not str or _ADMIN_CARD_ID.fullmatch(owner) is None or any(
        type(value) is not int or value <= 0 for value in values
    ):
        return None
    try:
        owner.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return _OwnerTransferBody(
        new_owner_user_id=owner,
        expected_card_revision=cast(int, values[0]),
        expected_assignment_generation=cast(int, values[1]),
        expected_assignment_revision=cast(int, values[2]),
    )


async def _owner_revoke_body(request: Request) -> _OwnerRevokeBody | None:
    payload = await _exact_json_body(request)
    if payload is None or set(payload) != {
        "reason_code", "expected_card_revision", "expected_assignment_generation",
        "expected_assignment_revision",
    }:
        return None
    reason = payload["reason_code"]
    values = tuple(payload[key] for key in (
        "expected_card_revision", "expected_assignment_generation", "expected_assignment_revision",
    ))
    if type(reason) is not str or _ADMIN_REASON_CODE.fullmatch(reason) is None or any(
        type(value) is not int or value <= 0 for value in values
    ):
        return None
    return _OwnerRevokeBody(
        reason_code=reason,
        expected_card_revision=cast(int, values[0]),
        expected_assignment_generation=cast(int, values[1]),
        expected_assignment_revision=cast(int, values[2]),
    )


def _policy_principal(
    composition: CentralComposition,
    request: Request,
    *,
    action: Literal["policy.read", "policy.write"],
) -> AuthenticatedPrincipal | Response:
    handle = request.cookies.get("__Host-aon-central-session")
    store = composition.browser_auth
    if type(handle) is not str or not handle:
        return _policy_error(401, "browser_session_unauthenticated")
    if store is None or composition.authority is None:
        return _policy_error(503, "policy_unavailable")
    try:
        outcome, session = store.read_current_session(
            opaque_browser_handle_digest(handle), now=_browser_now(composition)
        )
        if outcome is BrowserSessionCurrentOutcome.UNAUTHENTICATED or session is None:
            return _policy_error(401, "browser_session_unauthenticated")
        if outcome is not BrowserSessionCurrentOutcome.ACTIVE:
            return _policy_error(503, "policy_unavailable")
        principal = AuthenticatedPrincipal(
            org_id=session.org_id,
            subject_id=session.registry_user_id,
            identity_provider=composition.config.oidc_provider_id,
            identity_session_id=session.session_digest,
        )
        session_resource = ResourceRef(
            org_id=principal.org_id, kind="browser_session",
            resource_id=principal.identity_session_id, owner_subject_id=principal.subject_id,
        )
        if type(composition.authority.authorize(principal, "session.read", session_resource)) is not AuthorizationGrant:
            return _policy_error(403, "browser_session_forbidden")
        resource = ResourceRef(
            org_id=principal.org_id, kind="authority_policy", resource_id=principal.org_id,
        )
        if type(composition.authority.authorize(principal, action, resource)) is not AuthorizationGrant:
            return _policy_error(403, "policy_forbidden")
        return principal
    except Exception:
        return _policy_error(503, "policy_unavailable")


def _valid_request_path(request: Request, request_id: str) -> bool:
    if _REQUEST_ID.fullmatch(request_id) is None:
        return False
    raw_path = request.scope.get("raw_path", b"")
    if not isinstance(raw_path, bytes) or b"%2f" in raw_path.lower():
        return False
    return request.url.path in {
        "/v1/questions/" + request_id,
        "/v1/questions/" + request_id + "/stream",
        "/v1/questions/" + request_id + "/feedback",
    }


async def _feedback_body(request: Request) -> _FeedbackBody | None:
    payload = await _exact_json_body(request)
    if payload is None or set(payload) != {"record_id", "verdict", "comment"}:
        return None
    record_id, verdict, comment = payload["record_id"], payload["verdict"], payload["comment"]
    if type(record_id) is not str or not record_id.strip() or type(verdict) is not str or verdict not in {"good", "bad"}:
        return None
    try:
        comment_bytes = comment.encode("utf-8") if type(comment) is str else b""
    except UnicodeEncodeError:
        return None
    if type(comment) is not str or len(comment_bytes) > 4096:
        return None
    return _FeedbackBody(record_id=record_id, verdict=cast(Literal["good", "bad"], verdict), comment=comment)


async def _exact_json_body(request: Request) -> dict[str, object] | None:
    try:
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            return None
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > _MAX_BODY:
                return None
            chunks.append(chunk)
        raw = b"".join(chunks)
        payload = json.loads(raw, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
        return cast(dict[str, object], payload) if type(payload) is dict else None
    except Exception:
        return None


def _received_wire(request: QuestionRequest, replayed: bool) -> dict[str, object]:
    return {"request_id": request.request_id, "state": "received", "created_at": request.created_at.isoformat().replace("+00:00", "Z"), "replayed": replayed}


def _owned_lifecycle_projection(
    composition: CentralComposition, request_id: str, principal: AuthenticatedPrincipal,
) -> dict[str, object] | JSONResponse:
    store = composition.lifecycle_store
    if store is None:
        return _error(503, "question_lifecycle_unavailable")
    try:
        request = store.get(request_id)
        if request is None or request.org_id != principal.org_id or request.requester_id != principal.subject_id:
            return _error(404, "question_not_found")
        return _lifecycle_wire(store, request)
    except CentralQuestionLifecycleUnavailable:
        return _error(503, "question_lifecycle_unavailable")
    except Exception:
        return _error(503, "question_lifecycle_unavailable")


def _lifecycle_wire(store: object, request: QuestionRequest) -> dict[str, object]:
    state = request.state
    if isinstance(state, AnsweredRequest):
        projection = cast(AnsweredProjection | None, getattr(store, "answered_projection")(request.request_id))
        if projection is None:
            raise CentralQuestionLifecycleUnavailable()
        return projection.__dict__ if hasattr(projection, "__dict__") else {
            "type": projection.type, "request_id": projection.request_id, "state": projection.state,
            "retryable": projection.retryable, "record_id": projection.record_id, "text": projection.text,
            "answered_by": projection.answered_by, "mode": projection.mode, "sources": list(projection.sources),
            "review_status": projection.review_status,
        }
    if isinstance(state, DeclinedRequest):
        return {"type": "declined", "request_id": request.request_id, "state": "declined", "retryable": False,
                "reason_code": state.reason_code, "message": "요청을 처리할 수 없습니다."}
    if isinstance(state, FailedRequest):
        return {"type": "failed", "request_id": request.request_id, "state": "failed", "retryable": False,
                "error_code": state.error_code, "message": "요청 처리에 실패했습니다."}
    if isinstance(state, Received):
        kind, retryable = "routing", True
    elif isinstance(state, ReadyToDispatch):
        kind, retryable = "routed", True
    elif isinstance(state, AwaitingAnswer):
        kind, retryable = "routed", True
    elif isinstance(state, AwaitingApproval):
        kind, retryable = "routed", False
    elif isinstance(state, AwaitingConflict):
        kind, retryable = "contested", False
    else:
        kind, retryable = state.public_kind, False
    return {"type": "pending", "request_id": request.request_id, "state": state.kind, "kind": kind,
            "retryable": retryable, "message": "질문을 처리하고 있습니다."}


def _feedback_wire(result: QuestionFeedbackResult) -> dict[str, object]:
    return {"request_id": result.request_id, "record_id": result.record_id, "feedback_id": result.feedback_id,
            "verdict": result.verdict, "submitted_at": result.submitted_at.isoformat().replace("+00:00", "Z"),
            "replayed": result.replayed}


def _sse_frame(event_id: int, event: str, payload: dict[str, object]) -> str:
    return "id: " + str(event_id) + "\nevent: " + event + "\ndata: " + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ) + "\n\n"


def run_central_api(composition: CentralComposition, runner: CentralApiRunner) -> None:
    if type(composition) is not CentralComposition:
        raise TypeError("CentralComposition required")
    validate_central_installation_config(composition.config)
    try:
        runner(
            create_central_api_app(composition),
            host=composition.config.bind_host,
            port=composition.config.port,
        )
    finally:
        composition.close()


def uvicorn_runner(app: FastAPI, *, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, access_log=False)


async def _question_body(request: Request) -> str | None:
    values = await _exact_json_body(request)
    if values is None:
        return None
    if set(values) != {"question"}:
        return None
    question = values["question"]
    try:
        question_bytes = question.encode("utf-8") if type(question) is str else b""
    except UnicodeEncodeError:
        return None
    if type(question) is not str or not question.strip() or len(question_bytes) > _MAX_BODY:
        return None
    return question


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status_code)


def _policy_error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status_code, headers={"Cache-Control": "no-store"})


def _registry_error(status_code: int, code: str) -> JSONResponse:
    """Admission endpoints never serialize request, session, or evidence data."""
    return JSONResponse({"error": code}, status_code=status_code, headers={"Cache-Control": "no-store"})


def _registry_admission_factory(
    composition: CentralComposition, request: Request
) -> tuple[SessionDerivedRegistryRegistrationFactory, str] | JSONResponse:
    """Resolve only the opaque cookie to a request-local admission factory."""
    session_handle = request.cookies.get("__Host-aon-central-session")
    if type(session_handle) is not str or not session_handle:
        return _registry_error(401, "browser_session_unauthenticated")
    factory_builder = composition.registry_admission_factory
    browser_auth = composition.browser_auth
    browser_clock = composition.browser_clock
    if browser_auth is None or browser_clock is None or factory_builder is None:
        return _registry_error(503, "registry_registration_unavailable")
    try:
        digest = opaque_browser_handle_digest(session_handle)
        # This is deliberately an active/expiry/Registry-binding check only;
        # `BrowserSessionApplication.read()` would additionally require
        # session.read and accidentally make user.register depend on that
        # separate Authority action.  The scoped factory below checks the
        # route's exact action in its transaction (and again at precommit).
        outcome, _session = browser_auth.read_current_session(digest, now=browser_clock())
        if outcome is BrowserSessionCurrentOutcome.UNAUTHENTICATED:
            return _registry_error(401, "browser_session_unauthenticated")
        if outcome is not BrowserSessionCurrentOutcome.ACTIVE:
            return _registry_error(503, "registry_registration_unavailable")
        return factory_builder(digest), digest
    except Exception:
        return _registry_error(503, "registry_registration_unavailable")


async def _strict_registry_get_request(request: Request) -> bool:
    """GET admission calls accept one session cookie and no caller self-claims."""
    try:
        if tuple(request.query_params.multi_items()):
            return False
        if request.headers.get("content-length") not in {None, "0"}:
            return False
        if any(header in request.headers for header in _BROWSER_SELF_CLAIM_HEADERS):
            return False
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received:
                return False
        return True
    except Exception:
        return False


async def _strict_registry_post_envelope(request: Request, origin: str) -> bool:
    """Validate every browser-controlled envelope fact before body decoding."""
    try:
        if tuple(request.query_params.multi_items()):
            return False
        if any(header in request.headers for header in _BROWSER_SELF_CLAIM_HEADERS):
            return False
        if (
            request.headers.get("origin") != origin
            or request.headers.get("sec-fetch-site") != "same-origin"
            or request.headers.get("sec-fetch-mode") != "cors"
            or request.headers.get("sec-fetch-dest") != "empty"
        ):
            return False
        return True
    except Exception:
        return False


async def _registry_user_body(request: Request) -> _RegistryUserBody | None:
    try:
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            return None
        length = request.headers.get("content-length")
        if length is not None and (not length.isascii() or not length.isdecimal() or int(length) > 64 * 1024):
            return None
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > 64 * 1024:
                return None
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not 2 <= len(raw) <= 64 * 1024:
            return None
        payload: object = json.loads(
            raw, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError())
        )
        if type(payload) is not dict:
            return None
        body = cast(dict[object, object], payload)
        if set(body) != {"expected_revision", "user_id", "email", "manager"}:
            return None
        revision = body["expected_revision"]
        user_id = body["user_id"]
        email = body["email"]
        manager = body["manager"]
        if type(revision) is not int or revision < 0:
            return None
        if type(user_id) is not str or type(email) is not str:
            return None
        if manager is not None and type(manager) is not str:
            return None
        return _RegistryUserBody(
            expected_revision=revision,
            user_id=user_id,
            email=email,
            manager=manager,
        )
    except Exception:
        return None


async def _registry_card_body(request: Request) -> _RegistryCardBody | None:
    """Parse the exact Card registration DTO without accepting self-claims."""
    try:
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            return None
        length = request.headers.get("content-length")
        if length is not None and (not length.isascii() or not length.isdecimal() or int(length) > 64 * 1024):
            return None
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > 64 * 1024:
                return None
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not 2 <= len(raw) <= 64 * 1024:
            return None
        payload: object = json.loads(
            raw, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError())
        )
        if type(payload) is not dict:
            return None
        body = cast(dict[object, object], payload)
        fields = {
            "expected_revision", "agent_id", "owner", "team", "summary", "domains", "maintainer",
            "can_answer", "cannot_answer", "approval_when", "collaborate_when", "knowledge_sources",
            "trust_labels",
        }
        if set(body) != fields:
            return None
        revision = body["expected_revision"]
        if type(revision) is not int or revision < 0:
            return None
        string_fields = ("agent_id", "owner", "team", "summary")
        if any(type(body[field]) is not str for field in string_fields):
            return None
        maintainer = body["maintainer"]
        if maintainer is not None and type(maintainer) is not str:
            return None
        list_fields = (
            "domains", "can_answer", "cannot_answer", "approval_when", "collaborate_when",
            "knowledge_sources", "trust_labels",
        )
        if any(
            type(body[field]) is not list or any(type(value) is not str for value in cast(list[object], body[field]))
            for field in list_fields
        ):
            return None
        return _RegistryCardBody(
            expected_revision=revision,
            agent_id=cast(str, body["agent_id"]),
            owner=cast(str, body["owner"]),
            team=cast(str, body["team"]),
            summary=cast(str, body["summary"]),
            domains=cast(list[str], body["domains"]),
            maintainer=maintainer,
            can_answer=cast(list[str], body["can_answer"]),
            cannot_answer=cast(list[str], body["cannot_answer"]),
            approval_when=cast(list[str], body["approval_when"]),
            collaborate_when=cast(list[str], body["collaborate_when"]),
            knowledge_sources=cast(list[str], body["knowledge_sources"]),
            trust_labels=cast(list[str], body["trust_labels"]),
        )
    except Exception:
        return None


def _expired_transaction_error(status_code: int, code: str) -> JSONResponse:
    response = _error(status_code, code)
    response.delete_cookie("__Host-aon-central-oidc-tx", secure=True, httponly=True, samesite="lax", path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


def _browser_session_error(status_code: int, code: str) -> JSONResponse:
    response = _error(status_code, code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _browser_session_projection(projection: object) -> JSONResponse:
    from agent_org_network.central_browser_oidc import BrowserSessionProjection

    if type(projection) is not BrowserSessionProjection:
        return _browser_session_error(503, "browser_session_unavailable")
    return JSONResponse(
        {
            "authenticated": True,
            "registry_user_ref": projection.registry_user_ref,
            "expires_at": projection.expires_at.isoformat().replace("+00:00", "Z"),
            "actions": list(projection.actions),
        },
        headers={"Cache-Control": "no-store"},
    )


def _expired_browser_session_response() -> Response:
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie("__Host-aon-central-oidc-tx", secure=True, httponly=True, samesite="lax", path="/")
    response.delete_cookie("__Host-aon-central-session", secure=True, httponly=True, samesite="lax", path="/")
    response.delete_cookie("__Host-aon-central-csrf", secure=True, httponly=False, samesite="strict", path="/")
    return response


async def _strict_browser_start_request(request: Request, origin: str) -> bool:
    """Reject all browser-supplied identity and non-empty command material."""
    try:
        if tuple(request.query_params.multi_items()) or request.headers.get("content-length") not in {None, "0"}:
            return False
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received:
                return False
        headers = request.headers
        if (
            headers.get("origin") != origin
            or headers.get("sec-fetch-site") != "same-origin"
            or headers.get("sec-fetch-mode") != "navigate"
            or headers.get("sec-fetch-dest") != "document"
        ):
            return False
        return not any(
            header in headers
            for header in (
                "authorization", "x-aon-user", "x-aon-org", "x-aon-role",
                "x-aon-permission", "x-aon-token-claim",
            )
        )
    except Exception:
        return False


async def _strict_browser_logout_request(request: Request, origin: str) -> bool:
    """Require an empty same-origin fetch before looking up any session."""
    try:
        if tuple(request.query_params.multi_items()) or request.headers.get("content-length") not in {None, "0"}:
            return False
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received:
                return False
        headers = request.headers
        if (
            headers.get("origin") != origin
            or headers.get("sec-fetch-site") != "same-origin"
            or headers.get("sec-fetch-mode") != "cors"
            or headers.get("sec-fetch-dest") not in {"", "empty"}
        ):
            return False
        return not any(
            header in headers
            for header in (
                "authorization", "x-aon-user", "x-aon-org", "x-aon-role",
                "x-aon-permission", "x-aon-token-claim",
            )
        )
    except Exception:
        return False


async def _strict_browser_session_request(request: Request) -> bool:
    """A current-session read accepts its opaque cookie and no caller identity."""
    try:
        if tuple(request.query_params.multi_items()) or request.headers.get("content-length") not in {None, "0"}:
            return False
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received:
                return False
        return not any(
            header in request.headers
            for header in (
                "authorization", "x-aon-user", "x-aon-org", "x-aon-role",
                "x-aon-permission", "x-aon-token-claim",
            )
        )
    except Exception:
        return False


def _strict_callback_values(values: tuple[tuple[str, str], ...]) -> tuple[str | None, str | None, bool]:
    names = {name for name, _value in values}
    if len(values) == 2 and names == {"error", "state"}:
        mapping = dict(values)
        error, state = mapping["error"], mapping["state"]
        if 1 <= len(error) <= 128 and 1 <= len(state) <= 1024:
            return None, state, True
    if len(values) != 2 or names != {"code", "state"}:
        return None, None, False
    mapping = dict(values)
    code = mapping["code"]
    state = mapping["state"]
    if not 1 <= len(code) <= 2048 or not 1 <= len(state) <= 1024:
        return None, None, False
    return code, state, False
