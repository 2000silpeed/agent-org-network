"""Central Server HTTP surface for durable AuthoringRun metadata only."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import Cookie, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from pydantic import SecretStr

from agent_org_network.production_authoring_identity import (
    AuthoringIdentitySessionRef,
    AuthoringInvocation,
)
from agent_org_network.production_authoring_authorizer import (
    ProductionCentralTxCurrentAuthoringAuthorizer,
)
from agent_org_network.production_identity_sessions import (
    ProductionIdentityUnavailable,
    ProductionPrincipalResolver,
)
from agent_org_network.sqlite_production_authoring_runs import (
    AuthoringSourceRef,
    CompleteAuthoringRunCommand,
    BeginAuthoringRunPublishCommand,
    ReviewAuthoringRunCommand,
    ProductionAuthoringRunConflict,
    ProductionAuthoringRunDenied,
    ProductionAuthoringRunUnavailable,
    SqliteProductionAuthoringRuns,
    StartAuthoringRunCommand,
)

_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MAX_BODY = 64 * 1024


async def _bounded_json(request: Request) -> object:
    if request.headers.get("content-type") != "application/json":
        raise ValueError
    raw_length = request.headers.get("content-length")
    if raw_length is None or not raw_length.isdigit():
        raise ValueError
    declared = int(raw_length)
    if not 1 <= declared <= _MAX_BODY:
        raise ValueError
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


class _SourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_digest: str
    byte_size: int
    media_type: Literal["text/plain", "text/markdown"]


class _StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agent_id: str
    expected_card_revision: int
    expected_card_digest: str
    sources: list[_SourceBody]


class _CompleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    expected_revision: Literal[0]
    expected_card_revision: int
    expected_card_digest: str
    admitted_bundle_digest: str
    document_count: int
    edge_count: int
    dropped_count: int
    author_profile_digest: str


class _ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    expected_revision: Literal[1]
    expected_card_revision: int
    expected_card_digest: str
    concept_id: Literal["bundle"]
    source_digest: str
    draft_digest: str
    outcome: Literal["Approved", "Edited", "Rejected"]


class _PublishBeginBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    expected_revision: Literal[2]
    expected_card_revision: int
    expected_card_digest: str


def _mount_publish_begin_route(
    app: FastAPI,
    *,
    runs: SqliteProductionAuthoringRuns,
    invocation: Callable[[str | None], AuthoringInvocation],
) -> None:
    @app.post("/authoring/runs/publish-begin")
    async def publish_begin(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        idempotency_key: Annotated[str | None, Header()] = None,
        aon_identity_session: Annotated[str | None, Cookie()] = None,
    ) -> dict[str, object]:
        current = invocation(aon_identity_session)
        if idempotency_key is None or _OPAQUE.fullmatch(idempotency_key) is None:
            raise HTTPException(status_code=422, detail="Idempotency-Key required")
        try:
            body = _PublishBeginBody.model_validate(await _bounded_json(request))
            result = runs.begin_publish(BeginAuthoringRunPublishCommand(
                organization_id=current.org_id, principal_id=current.principal_id,
                idempotency_key=idempotency_key, **body.model_dump(),
            ), invocation=current)
        except ProductionAuthoringRunDenied:
            raise HTTPException(status_code=403, detail="Denied") from None
        except ProductionAuthoringRunConflict:
            raise HTTPException(status_code=409, detail="Conflict") from None
        except ProductionAuthoringRunUnavailable:
            raise HTTPException(status_code=503, detail="Unavailable") from None
        except Exception as error:
            from pydantic import ValidationError
            if isinstance(error, (ValidationError, ValueError)):
                raise HTTPException(status_code=422, detail="Invalid request") from None
            raise HTTPException(status_code=503, detail="Unavailable") from None
        return {"run": result.run.model_dump(mode="json"), "replayed": result.replayed}


def create_central_authoring_app(
    *,
    runs: SqliteProductionAuthoringRuns,
    principal_resolver: ProductionPrincipalResolver,
    authorizer: ProductionCentralTxCurrentAuthoringAuthorizer,
    include_publish_begin: bool = True,
) -> FastAPI:
    if (
        type(runs) is not SqliteProductionAuthoringRuns
        or type(principal_resolver) is not ProductionPrincipalResolver
        or type(authorizer) is not ProductionCentralTxCurrentAuthoringAuthorizer
        or getattr(runs, "_authorize", None) is not authorizer
        or type(include_publish_begin) is not bool
    ):
        raise ProductionAuthoringRunUnavailable()
    app = FastAPI()

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
            raise HTTPException(status_code=401, detail="Authentication required") from None

    @app.post("/authoring/runs/start")
    async def start(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        idempotency_key: Annotated[str | None, Header()] = None,
        aon_identity_session: Annotated[str | None, Cookie()] = None,
    ) -> dict[str, object]:
        current = invocation(aon_identity_session)
        if idempotency_key is None or _OPAQUE.fullmatch(idempotency_key) is None:
            raise HTTPException(status_code=422, detail="Idempotency-Key required")
        try:
            body = _StartBody.model_validate(await _bounded_json(request))
            result = runs.start(
                StartAuthoringRunCommand(
                    org_id=current.org_id,
                    principal_id=current.principal_id,
                    idempotency_key=idempotency_key,
                    agent_id=body.agent_id,
                    expected_card_revision=body.expected_card_revision,
                    expected_card_digest=body.expected_card_digest,
                    sources=tuple(
                        AuthoringSourceRef(**source.model_dump()) for source in body.sources
                    ),
                ),
                invocation=current,
            )
        except ProductionAuthoringRunDenied:
            raise HTTPException(status_code=403, detail="Denied") from None
        except ProductionAuthoringRunConflict:
            raise HTTPException(status_code=409, detail="Conflict") from None
        except ProductionAuthoringRunUnavailable:
            raise HTTPException(status_code=503, detail="Unavailable") from None
        except Exception as error:
            from pydantic import ValidationError

            if isinstance(error, (ValidationError, ValueError)):
                raise HTTPException(status_code=422, detail="Invalid request") from None
            raise HTTPException(status_code=503, detail="Unavailable") from None
        return {"run": result.run.model_dump(mode="json"), "replayed": result.replayed}

    @app.post("/authoring/runs/complete")
    async def complete(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        idempotency_key: Annotated[str | None, Header()] = None,
        aon_identity_session: Annotated[str | None, Cookie()] = None,
    ) -> dict[str, object]:
        current = invocation(aon_identity_session)
        if idempotency_key is None or _OPAQUE.fullmatch(idempotency_key) is None:
            raise HTTPException(status_code=422, detail="Idempotency-Key required")
        try:
            body = _CompleteBody.model_validate(await _bounded_json(request))
            result = runs.complete(
                CompleteAuthoringRunCommand(
                    organization_id=current.org_id,
                    principal_id=current.principal_id,
                    idempotency_key=idempotency_key,
                    **body.model_dump(),
                ),
                invocation=current,
            )
        except ProductionAuthoringRunDenied:
            raise HTTPException(status_code=403, detail="Denied") from None
        except ProductionAuthoringRunConflict:
            raise HTTPException(status_code=409, detail="Conflict") from None
        except ProductionAuthoringRunUnavailable:
            raise HTTPException(status_code=503, detail="Unavailable") from None
        except Exception as error:
            from pydantic import ValidationError

            if isinstance(error, (ValidationError, ValueError)):
                raise HTTPException(status_code=422, detail="Invalid request") from None
            raise HTTPException(status_code=503, detail="Unavailable") from None
        return {"run": result.run.model_dump(mode="json"), "replayed": result.replayed}

    @app.post("/authoring/runs/review")
    async def review(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        idempotency_key: Annotated[str | None, Header()] = None,
        aon_identity_session: Annotated[str | None, Cookie()] = None,
    ) -> dict[str, object]:
        current = invocation(aon_identity_session)
        if idempotency_key is None or _OPAQUE.fullmatch(idempotency_key) is None:
            raise HTTPException(status_code=422, detail="Idempotency-Key required")
        try:
            body = _ReviewBody.model_validate(await _bounded_json(request))
            result = runs.review(ReviewAuthoringRunCommand(
                organization_id=current.org_id, principal_id=current.principal_id,
                idempotency_key=idempotency_key, **body.model_dump(),
            ), invocation=current)
        except ProductionAuthoringRunDenied:
            raise HTTPException(status_code=403, detail="Denied") from None
        except ProductionAuthoringRunConflict:
            raise HTTPException(status_code=409, detail="Conflict") from None
        except ProductionAuthoringRunUnavailable:
            raise HTTPException(status_code=503, detail="Unavailable") from None
        except Exception as error:
            from pydantic import ValidationError
            if isinstance(error, (ValidationError, ValueError)):
                raise HTTPException(status_code=422, detail="Invalid request") from None
            raise HTTPException(status_code=503, detail="Unavailable") from None
        return {"run": result.run.model_dump(mode="json"), "replayed": result.replayed}

    if include_publish_begin:
        _mount_publish_begin_route(app, runs=runs, invocation=invocation)

    return app


__all__ = ["create_central_authoring_app"]
