"""HTTP composition hosted by the Card Owner installation, never centrally."""

from __future__ import annotations

from base64 import b64decode
from collections.abc import Callable
from dataclasses import dataclass
import json
import re
from typing import Annotated, Literal, Protocol

from fastapi import Cookie, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, field_validator

from agent_org_network.central_authoring_client import (
    ProductionCentralAuthoringClient,
)
from agent_org_network.okf_authoring import LlmAuthor, OkfAuthor
from agent_org_network.owner_authoring_adapter import (
    CentralAuthoringRuns,
    OwnerAuthoringDocument,
    OwnerAuthoringRequest,
    OwnerAuthoringUnavailable,
    run_owner_authoring,
)
from agent_org_network.owner_local_authoring_repository import (
    OwnerLocalAuthoringRepository,
)
from agent_org_network.owner_authoring_operation_store import (
    OwnerAuthoringOperationStore,
)
from agent_org_network.production_authoring_identity import AuthoringInvocation


class _DocumentBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str
    media_type: Literal["text/plain", "text/markdown"]
    content_base64: str

    @field_validator("content_base64")
    @classmethod
    def _bounded(cls, value: str) -> str:
        if not 1 <= len(value) <= 140 * 1024 * 1024:
            raise ValueError("bounded encoded document required")
        return value


class _RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agent_id: str
    documents: list[_DocumentBody]

    @field_validator("documents")
    @classmethod
    def _document_count(cls, value: list[_DocumentBody]) -> list[_DocumentBody]:
        if not 1 <= len(value) <= 32:
            raise ValueError("bounded document count required")
        return value


OwnerRequestBinding = Callable[
    [
        str,
        str,
        str,
        tuple[OwnerAuthoringDocument, ...],
    ],
    tuple[OwnerAuthoringRequest, AuthoringInvocation],
]
_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
MAX_OWNER_HTTP_BODY_BYTES = 140 * 1024 * 1024


class OwnerWebRequestVerifier(Protocol):
    def verify(self, owner_session: str, csrf_proof: str, origin: str) -> bool: ...


class OwnerAuthoringProductionUnavailable(Exception):
    pass


@dataclass(frozen=True, slots=True)
class OwnerAuthoringProductionWiring:
    central: ProductionCentralAuthoringClient
    repository: OwnerLocalAuthoringRepository
    operations: OwnerAuthoringOperationStore
    author: LlmAuthor

    def __post_init__(self) -> None:
        if (
            type(self.central) is not ProductionCentralAuthoringClient
            or type(self.repository) is not OwnerLocalAuthoringRepository
            or type(self.operations) is not OwnerAuthoringOperationStore
            or type(self.author) is not LlmAuthor
        ):
            raise OwnerAuthoringProductionUnavailable()


def create_owner_authoring_app(
    wiring: OwnerAuthoringProductionWiring,
) -> FastAPI:
    """Production composition stays closed until O3a.5c pairing is concrete."""
    if type(wiring) is not OwnerAuthoringProductionWiring:
        raise OwnerAuthoringProductionUnavailable()
    raise OwnerAuthoringProductionUnavailable()


def _create_owner_authoring_test_app(  # pyright: ignore[reportUnusedFunction]
    *,
    bind_request: OwnerRequestBinding,
    central: CentralAuthoringRuns,
    repository: OwnerLocalAuthoringRepository,
    operations: OwnerAuthoringOperationStore,
    author: OkfAuthor,
    request_verifier: OwnerWebRequestVerifier,
) -> FastAPI:
    """Build the owner-local API with no Fake/InMemory/default capability."""
    app = FastAPI()

    @app.post("/authoring/runs")
    async def create_run(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        idempotency_key: Annotated[str | None, Header()] = None,
        x_owner_csrf: Annotated[str | None, Header()] = None,
        origin: Annotated[str | None, Header()] = None,
        aon_owner_session: Annotated[str | None, Cookie()] = None,
    ) -> dict[str, object]:
        if (
            aon_owner_session is None
            or _OPAQUE.fullmatch(aon_owner_session) is None
            or x_owner_csrf is None
            or _OPAQUE.fullmatch(x_owner_csrf) is None
            or origin is None
            or not 1 <= len(origin) <= 512
        ):
            raise HTTPException(status_code=401, detail="Owner pairing required")
        try:
            verified = request_verifier.verify(
                aon_owner_session, x_owner_csrf, origin
            )
        except Exception:
            raise HTTPException(
                status_code=503, detail="Owner authoring unavailable"
            ) from None
        if not verified:
            raise HTTPException(status_code=401, detail="Owner pairing required")
        if idempotency_key is None or _OPAQUE.fullmatch(idempotency_key) is None:
            raise HTTPException(status_code=422, detail="Idempotency-Key required")
        try:
            if request.headers.get("content-type") != "application/json":
                raise ValueError
            raw_length = request.headers.get("content-length")
            if raw_length is None or not raw_length.isascii() or not raw_length.isdigit():
                raise ValueError
            content_length = int(raw_length)
            if not 1 <= content_length <= MAX_OWNER_HTTP_BODY_BYTES:
                raise ValueError
            chunks: list[bytes] = []
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > MAX_OWNER_HTTP_BODY_BYTES or received > content_length:
                    raise ValueError
                chunks.append(chunk)
            if received != content_length:
                raise ValueError
            body = _RunBody.model_validate(json.loads(b"".join(chunks)))
            documents = tuple(
                OwnerAuthoringDocument(
                    source_id=document.source_id,
                    media_type=document.media_type,
                    content=b64decode(document.content_base64, validate=True),
                )
                for document in body.documents
            )
            command, invocation = bind_request(
                aon_owner_session, body.agent_id, idempotency_key, documents
            )
            result = run_owner_authoring(
                command,
                invocation=invocation,
                central=central,
                repository=repository,
                operations=operations,
                author=author,
            )
        except OwnerAuthoringUnavailable:
            raise HTTPException(
                status_code=503, detail="Owner authoring unavailable"
            ) from None
        except Exception as error:
            from pydantic import ValidationError

            if isinstance(error, (ValidationError, ValueError)):
                raise HTTPException(
                    status_code=422, detail="Invalid owner authoring request"
                ) from None
            raise HTTPException(
                status_code=503, detail="Owner authoring unavailable"
            ) from None
        return {
            "run_id": result.run_id,
            "stage": result.stage,
            "revision": result.revision,
            "document_count": result.document_count,
            "edge_count": result.edge_count,
            "dropped_count": result.dropped_count,
        }

    return app


__all__ = [
    "MAX_OWNER_HTTP_BODY_BYTES",
    "OwnerAuthoringProductionUnavailable",
    "OwnerAuthoringProductionWiring",
    "create_owner_authoring_app",
]
